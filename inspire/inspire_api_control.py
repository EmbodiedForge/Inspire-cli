#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
启智(Inspire)API控制脚本 - 智能资源匹配版本
Inspire API Control Script - Smart Resource Matching Version

This script provides functionality to:
- Authenticate with the Inspire API
- Create distributed training jobs with smart resource matching
- Query training job details
- Stop training jobs
- List cluster nodes

New Features:
- Natural language resource specification (e.g., "H200", "H100", "4xH200")
- Automatic spec-id and compute-group-id matching
- Interactive resource selection
- Enhanced user experience

API Documentation: https://qz.sii.edu.cn/openapi/
"""

import os
import json
import logging
import requests
import argparse
import time
import re
from typing import Dict, Any, Optional, Union, Tuple, List
from dataclasses import dataclass
from enum import Enum

# Suppress SSL warnings when verification is disabled
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class GPUType(Enum):
    """GPU类型枚举"""
    H100 = "H100"
    H200 = "H200"


@dataclass
class ResourceSpec:
    """资源规格配置"""
    gpu_type: GPUType
    gpu_count: int
    cpu_cores: int
    memory_gb: int
    gpu_memory_gb: int
    spec_id: str
    description: str


@dataclass
class ComputeGroup:
    """计算类型组配置"""
    name: str
    compute_group_id: str
    gpu_type: GPUType
    location: str = ""


@dataclass
class InspireConfig:
    """Inspire API 配置类"""
    base_url: str = "https://qz.sii.edu.cn"
    timeout: int = 30
    max_retries: int = 3
    retry_delay: float = 1.0
    verify_ssl: bool = True  # Can be disabled via INSPIRE_SKIP_SSL_VERIFY env var


class ResourceManager:
    """资源管理器 - 处理资源规格和计算组的匹配"""
    
    def __init__(self):
        # 定义可用的资源规格
        self.resource_specs = [
            ResourceSpec(
                gpu_type=GPUType.H200,
                gpu_count=1,
                cpu_cores=15,
                memory_gb=200,
                gpu_memory_gb=141,
                spec_id="4dd0e854-e2a4-4253-95e6-64c13f0b5117",
                description="1 × NVIDIA H200 (141GB) + 15核CPU + 200GB内存"
            ),
            ResourceSpec(
                gpu_type=GPUType.H200,
                gpu_count=4,
                cpu_cores=60,
                memory_gb=800,
                gpu_memory_gb=141,
                spec_id="45ab2351-fc8a-4d50-a30b-b39a5306c906",
                description="4 × NVIDIA H200 (141GB) + 60核CPU + 800GB内存"
            ),
            ResourceSpec(
                gpu_type=GPUType.H200,
                gpu_count=8,
                cpu_cores=120,
                memory_gb=1600,
                gpu_memory_gb=141,
                spec_id="b618f5cb-c119-4422-937e-f39131853076",
                description="8 × NVIDIA H200 (141GB) + 120核CPU + 1600GB内存"
            )
        ]
        
        # 定义可用的计算类型组
        self.compute_groups = [
            ComputeGroup(
                name="H100 (CUDA 12.8)",
                compute_group_id="lcg-79b2ad0e-a375-43f3-a0b1-b4ce79710fd7",
                gpu_type=GPUType.H100,
                location="CUDA 12.8版本"
            ),
            ComputeGroup(
                name="H200 机房1",
                compute_group_id="lcg-df089db8-817a-4aa8-a164-eb1a32948564",
                gpu_type=GPUType.H200,
                location="1号机房"
            ),
            ComputeGroup(
                name="H200 机房2",
                compute_group_id="lcg-303ac8c6-aa19-4284-af03-2296592326e5",
                gpu_type=GPUType.H200,
                location="2号机房"
            ),
            ComputeGroup(
                name="H200 机房3",
                compute_group_id="lcg-a91ad10b-415d-4abd-8170-828a2feae5d2",
                gpu_type=GPUType.H200,
                location="3号机房"
            )
        ]
    
    def parse_resource_request(self, resource_str: str) -> Tuple[GPUType, int]:
        """
        解析自然语言的资源请求
        
        Args:
            resource_str: 资源描述字符串，如 "H200", "4xH200", "8 H100"
            
        Returns:
            (GPU类型, GPU数量) 元组
            
        Raises:
            ValueError: 无法解析资源请求时
        """
        if not resource_str:
            raise ValueError("资源描述不能为空")
        
        # 清理并转换为大写
        resource_str = resource_str.upper().strip()
        
        # 匹配模式: 数字 + x/X + GPU类型, 或者 数字 + 空格 + GPU类型, 或者直接GPU类型
        patterns = [
            r'^(\d+)[xX]?(H100|H200)$',  # "4xH200", "4H200", "4 H200"
            r'^(H100|H200)[xX]?(\d+)?$',  # "H200", "H200x4", "H200 4"
            r'^(\d+)\s+(H100|H200)$',     # "4 H200"
        ]
        
        gpu_count = 1  # 默认数量
        gpu_type_str = None
        
        for pattern in patterns:
            match = re.match(pattern, resource_str.replace(' ', ''))
            if match:
                groups = match.groups()
                if len(groups) == 2:
                    # 可能是 (数字, GPU类型) 或 (GPU类型, 数字)
                    if groups[0].isdigit():
                        gpu_count = int(groups[0])
                        gpu_type_str = groups[1]
                    elif groups[1] and groups[1].isdigit():
                        gpu_type_str = groups[0]
                        gpu_count = int(groups[1])
                    else:
                        gpu_type_str = groups[0] if not groups[0].isdigit() else groups[1]
                break
        
        # 如果没有匹配到数字+GPU模式，尝试直接匹配GPU类型
        if not gpu_type_str:
            if 'H200' in resource_str:
                gpu_type_str = 'H200'
            elif 'H100' in resource_str:
                gpu_type_str = 'H100'
        
        if not gpu_type_str:
            raise ValueError(f"无法识别的GPU类型: {resource_str}")
        
        try:
            gpu_type = GPUType(gpu_type_str)
        except ValueError:
            raise ValueError(f"不支持的GPU类型: {gpu_type_str}，支持的类型: H100, H200")
        
        if gpu_count <= 0:
            raise ValueError(f"GPU数量必须为正数: {gpu_count}")
        
        return gpu_type, gpu_count
    
    def find_matching_specs(self, gpu_type: GPUType, gpu_count: int) -> List[ResourceSpec]:
        """
        查找匹配的资源规格
        
        Args:
            gpu_type: GPU类型
            gpu_count: 所需GPU数量
            
        Returns:
            匹配的资源规格列表
        """
        matching_specs = []
        
        for spec in self.resource_specs:
            # 对于H100，由于spec_id相同，可以使用H200的规格
            if (spec.gpu_type == gpu_type or 
                (gpu_type == GPUType.H100 and spec.gpu_type == GPUType.H200)):
                if spec.gpu_count >= gpu_count:
                    matching_specs.append(spec)
        
        # 按GPU数量排序，优先选择最接近需求的配置
        matching_specs.sort(key=lambda x: x.gpu_count)
        return matching_specs
    
    def find_compute_groups(self, gpu_type: GPUType) -> List[ComputeGroup]:
        """
        查找匹配的计算类型组
        
        Args:
            gpu_type: GPU类型
            
        Returns:
            匹配的计算类型组列表
        """
        return [group for group in self.compute_groups if group.gpu_type == gpu_type]
    
    def get_recommended_config(self, resource_str: str, prefer_location: Optional[str] = None) -> Tuple[str, str]:
        """
        获取推荐的配置
        
        Args:
            resource_str: 资源描述字符串
            prefer_location: 偏好的机房位置
            
        Returns:
            (spec_id, compute_group_id) 元组
            
        Raises:
            ValueError: 无法找到匹配配置时
        """
        gpu_type, gpu_count = self.parse_resource_request(resource_str)
        
        # 查找匹配的规格
        matching_specs = self.find_matching_specs(gpu_type, gpu_count)
        if not matching_specs:
            available_configs = [f"{spec.gpu_count}x{spec.gpu_type.value}" 
                               for spec in self.resource_specs]
            raise ValueError(
                f"没有找到匹配 {gpu_count}x{gpu_type.value} 的配置。"
                f"可用配置: {', '.join(available_configs)}"
            )
        
        # 选择最合适的规格（最小的满足需求的配置）
        selected_spec = matching_specs[0]
        
        # 查找匹配的计算组
        matching_groups = self.find_compute_groups(gpu_type)
        if not matching_groups:
            raise ValueError(f"没有找到支持 {gpu_type.value} 的计算类型组")
        
        # 选择计算组（优先考虑位置偏好）
        selected_group = matching_groups[0]  # 默认选择第一个
        
        if prefer_location:
            for group in matching_groups:
                if prefer_location.lower() in group.location.lower():
                    selected_group = group
                    break
        
        return selected_spec.spec_id, selected_group.compute_group_id
    
    def display_available_resources(self) -> None:
        """显示所有可用的资源配置"""
        print("\n📊 可用资源配置:")
        print("=" * 60)
        
        print("\n🖥️  GPU规格配置:")
        for spec in self.resource_specs:
            print(f"  • {spec.description}")
            print(f"    Spec ID: {spec.spec_id}")
        
        print("\n🏢 计算类型组:")
        for group in self.compute_groups:
            print(f"  • {group.name} ({group.location})")
            print(f"    Compute Group ID: {group.compute_group_id}")
        
        print("\n💡 使用示例:")
        print("  • --resource 'H200'     -> 1个H200 GPU")
        print("  • --resource '4xH200'   -> 4个H200 GPU")
        print("  • --resource '8 H200'   -> 8个H200 GPU")
        print("  • --resource 'H100'     -> 1个H100 GPU")
        print("=" * 60)


class APIEndpoints:
    """API 端点常量"""
    AUTH_TOKEN = "/auth/token"
    TRAIN_JOB_CREATE = "/openapi/v1/train_job/create"
    TRAIN_JOB_DETAIL = "/openapi/v1/train_job/detail"
    TRAIN_JOB_STOP = "/openapi/v1/train_job/stop"
    SPECS_LIST = "/openapi/v1/specs/list"
    CLUSTER_NODES_LIST = "/openapi/v1/cluster_nodes/list"


class InspireAPIError(Exception):
    """Inspire API 基础异常"""
    pass


class AuthenticationError(InspireAPIError):
    """认证失败异常"""
    pass


class JobCreationError(InspireAPIError):
    """任务创建失败异常"""
    pass


class ValidationError(InspireAPIError):
    """输入验证失败异常"""
    pass


class InspireAPI:
    """
    启智API客户端 - 智能资源匹配版
    Inspire API Client - Smart Resource Matching Version
    """
    
    # 默认值常量
    DEFAULT_TASK_PRIORITY = 8
    DEFAULT_INSTANCE_COUNT = 1
    DEFAULT_SHM_SIZE = 40
    DEFAULT_MAX_RUNNING_TIME = "360000000"  # 100小时
    DEFAULT_IMAGE_TYPE = "SOURCE_PRIVATE"
    DEFAULT_PROJECT_ID = os.getenv(
        'INSPIRE_PROJECT_ID',
        "project-c67c548f-f02c-453b-ba5b-8745db6886e7" # Placeholder from EBM_dev
    )
    DEFAULT_WORKSPACE_ID = os.getenv(
        'INSPIRE_WORKSPACE_ID',
        "ws-9dcc0e1f-80a4-4af2-bc2f-0e352e7b17e6" # Placeholder from EBM_dev
    )
    DEFAULT_IMAGE = "docker.sii.shaipower.online/inspire-studio/ngc-cuda12.8-base:1.0"
    ERROR_BODY_PREVIEW_LIMIT = 4000

    def __init__(self, config: Optional[InspireConfig] = None):
        """
        初始化API客户端
        
        Args:
            config: API配置对象，如果为None则使用默认配置
        """
        self.config = config or InspireConfig()
        
        # Check for SSL verification override via environment variable
        if os.getenv('INSPIRE_SKIP_SSL_VERIFY', '').lower() in ('1', 'true', 'yes'):
            self.config.verify_ssl = False

        self.base_url = self.config.base_url.rstrip('/')
        self.token = None
        self.headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }
        
        # 初始化资源管理器
        self.resource_manager = ResourceManager()
        
        # 使用简单的requests session
        self.session = requests.Session()
        # Enable proxy and no_proxy support from environment by default
        self.session.trust_env = True

        # Optional override: force using proxy even if no_proxy would normally bypass it.
        # This preserves the previous WSL corporate-proxy workaround when needed.
        if os.getenv('INSPIRE_FORCE_PROXY', '').lower() in ('1', 'true', 'yes'):
            http_proxy = os.environ.get('http_proxy') or os.environ.get('HTTP_PROXY')
            https_proxy = os.environ.get('https_proxy') or os.environ.get('HTTPS_PROXY')
            if http_proxy or https_proxy:
                self.session.proxies = {
                    'http': http_proxy or https_proxy,
                    'https': https_proxy or http_proxy,
                }
                logger.debug(f"INSPIRE_FORCE_PROXY enabled, using explicit proxy configuration: {self.session.proxies}")
    
    def _validate_required_params(self, **kwargs) -> None:
        """验证必需参数"""
        for param_name, param_value in kwargs.items():
            if param_value is None or (isinstance(param_value, str) and not param_value.strip()):
                raise ValidationError(f"Required parameter '{param_name}' cannot be empty")
    
    def _make_request_with_retry(self, method: str, url: str, **kwargs) -> requests.Response:
        """
        带重试机制的请求方法
        """
        last_exception = None
        # Add SSL verification setting to kwargs if not already present
        if 'verify' not in kwargs:
            kwargs['verify'] = self.config.verify_ssl

        for attempt in range(self.config.max_retries + 1):
            try:
                if method.upper() == 'POST':
                    response = self.session.post(url, timeout=self.config.timeout, **kwargs)
                else:
                    response = self.session.get(url, timeout=self.config.timeout, **kwargs)

                if response.status_code < 500:
                    return response
                else:
                    if attempt < self.config.max_retries:
                        logger.warning(f"Server error {response.status_code}, retrying in {self.config.retry_delay}s...")
                        time.sleep(self.config.retry_delay * (attempt + 1))
                        continue
                    else:
                        response.raise_for_status()

            except requests.exceptions.Timeout as e:
                last_exception = e
                if attempt < self.config.max_retries:
                    logger.warning(f"Request timeout, retrying in {self.config.retry_delay}s...")
                    time.sleep(self.config.retry_delay * (attempt + 1))
                    continue
                else:
                    raise InspireAPIError(f"Request timeout after {self.config.max_retries} retries")

            except requests.exceptions.ConnectionError as e:
                last_exception = e
                if attempt < self.config.max_retries:
                    logger.warning(f"Connection error, retrying in {self.config.retry_delay}s...")
                    time.sleep(self.config.retry_delay * (attempt + 1))
                    continue
                else:
                    raise InspireAPIError(f"Connection error after {self.config.max_retries} retries: {str(e)}")
            
            except requests.exceptions.SSLError as e:
                last_exception = e
                if not self.config.verify_ssl:
                    logger.warning(f"SSL error detected, but SSL verification is disabled. This may be normal with corporate proxies.")
                if attempt < self.config.max_retries:
                    logger.warning(f"SSL error, retrying in {self.config.retry_delay}s...")
                    time.sleep(self.config.retry_delay * (attempt + 1))
                    continue
                else:
                    error_msg = str(e)
                    if not self.config.verify_ssl:
                        error_msg += "\n💡 Hint: SSL verification is disabled (INSPIRE_SKIP_SSL_VERIFY=1). If this persists, check your proxy settings or firewall."
                    raise InspireAPIError(f"SSL error after {self.config.max_retries} retries: {error_msg}")

            except requests.exceptions.RequestException as e:
                raise InspireAPIError(f"Request failed: {str(e)}")

        if last_exception:
            raise InspireAPIError(f"All retry attempts failed. Last error: {str(last_exception)}")
        else:
            raise InspireAPIError("All retry attempts failed")

    def _summarize_response_error(self, response: Optional[requests.Response]) -> str:
        """格式化HTTP错误响应，包含状态、URL、响应头和截断的响应体"""
        if response is None:
            return "No HTTP response available."
        headers = {k: v for k, v in response.headers.items()}
        body_preview = response.text or ""
        truncated = False
        if len(body_preview) > self.ERROR_BODY_PREVIEW_LIMIT:
            body_preview = body_preview[:self.ERROR_BODY_PREVIEW_LIMIT]
            truncated = True
        summary_lines = [
            f"Status: {response.status_code} {response.reason}",
            f"URL: {response.url}",
            f"Headers: {json.dumps(headers, ensure_ascii=False)}",
            "Body:",
            (body_preview.strip() or "<empty>")
        ]
        if truncated:
            summary_lines.append(f"... (truncated to {self.ERROR_BODY_PREVIEW_LIMIT} characters)")
        return "\n".join(summary_lines)

    def _make_request(self, method: str, endpoint: str, payload: Optional[Dict] = None) -> Dict[str, Any]:
        """发送HTTP请求的通用方法"""
        url = f"{self.base_url}{endpoint}"
        response: Optional[requests.Response] = None

        try:
            kwargs = {'headers': self.headers}
            if payload is not None:
                kwargs['json'] = payload

            response = self._make_request_with_retry(method, url, **kwargs)

            logger.debug(f"Request: {method} {url}")
            logger.debug(f"Response status: {response.status_code}")

            response.raise_for_status()
            result = response.json()

            if not isinstance(result, dict) or 'code' not in result:
                raise InspireAPIError("Invalid API response format")

            return result

        except requests.exceptions.HTTPError as http_err:
            error_summary = self._summarize_response_error(http_err.response or response)
            logger.error("❌ Inspire API returned non-success response.\n%s", error_summary)
            raise InspireAPIError(f"HTTP error while requesting {endpoint}: {error_summary}") from http_err
        except json.JSONDecodeError:
            body_preview = (response.text[:self.ERROR_BODY_PREVIEW_LIMIT] + "..."
                            if response and len(response.text) > self.ERROR_BODY_PREVIEW_LIMIT
                            else (response.text if response else "<no response>"))
            raise InspireAPIError(f"Invalid JSON response from API. Body preview: {body_preview}")
        except requests.exceptions.RequestException as e:
            raise InspireAPIError(f"Request failed: {str(e)}")
    
    def authenticate(self, username: str, password: str) -> bool:
        """使用用户名和密码获取访问令牌"""
        self._validate_required_params(username=username, password=password)
        
        payload = {
            "username": username,
            "password": password
        }
        
        try:
            result = self._make_request('POST', APIEndpoints.AUTH_TOKEN, payload)
            
            if result.get('code') == 0:
                self.token = result['data']['access_token']
                self.headers['Authorization'] = f"Bearer {self.token}"
                expires_in = result['data'].get('expires_in', 'unknown')
                logger.info(f"🔐 Authentication successful. Token expires in {expires_in} seconds.")
                return True
            else:
                error_msg = result.get('message', 'Unknown authentication error')
                raise AuthenticationError(f"Authentication failed: {error_msg}")
                
        except InspireAPIError as e:
            if "Authentication failed" in str(e):
                raise
            raise AuthenticationError(f"Authentication request failed: {str(e)}")
    
    def _check_authentication(self) -> None:
        """检查是否已认证"""
        if not self.token:
            raise AuthenticationError("Not authenticated. Please authenticate first.")
    
    def create_training_job_smart(self, 
                                name: str,
                                command: str,
                                resource: str,
                                framework: str = "pytorch",
                                prefer_location: Optional[str] = None,
                                project_id: Optional[str] = None,
                                workspace_id: Optional[str] = None,
                                image: Optional[str] = None,
                                task_priority: int = DEFAULT_TASK_PRIORITY,
                                instance_count: int = DEFAULT_INSTANCE_COUNT,
                                shm_gi: int = DEFAULT_SHM_SIZE,
                                max_running_time_ms: str = DEFAULT_MAX_RUNNING_TIME,
                                auto_fault_tolerance: bool = False,
                                enable_notification: bool = False,
                                enable_troubleshoot: bool = False,
                                **kwargs) -> Dict[str, Any]:
        """
        智能创建分布式训练任务
        
        Args:
            name: 训练任务名称
            command: 启动命令
            resource: 资源描述 (如: "H200", "4xH200", "8 H200")
            framework: 训练框架 (默认: pytorch)
            prefer_location: 偏好的机房位置 (如: "1号", "2号", "3号")
            project_id: 项目ID (可选，使用默认值)
            workspace_id: 工作空间ID (可选，使用默认值)
            image: 镜像名称 (可选，使用默认值)
            task_priority: 任务优先级 (默认: 8)
            instance_count: 实例数量 (默认: 1)
            shm_gi: 共享内存大小 (默认: 40)
            max_running_time_ms: 最大运行时间(毫秒) (默认: 360000000ms=100h)
            auto_fault_tolerance: 是否开启容错 (默认: False)
            enable_notification: 是否启用通知 (默认: False)
            enable_troubleshoot: 是否启用故障排除 (默认: False)
            
        Returns:
            API响应数据
            
        Raises:
            ValidationError: 参数验证失败时
            JobCreationError: 任务创建失败时
            AuthenticationError: 未认证时
        """
        self._check_authentication()
        
        # 验证必需参数
        self._validate_required_params(name=name, command=command, resource=resource)
        
        # 智能匹配资源配置
        try:
            spec_id, compute_group_id = self.resource_manager.get_recommended_config(
                resource, prefer_location
            )
            logger.info(f"🎯 Smart resource matching:")
            logger.info(f"   Resource: {resource}")
            logger.info(f"   Spec ID: {spec_id}")
            logger.info(f"   Compute Group ID: {compute_group_id}")
        except ValueError as e:
            raise ValidationError(f"Resource matching failed: {str(e)}")
        
        # 使用默认值填充可选参数
        project_id = project_id or self.DEFAULT_PROJECT_ID
        workspace_id = workspace_id or self.DEFAULT_WORKSPACE_ID
        image = image or self.DEFAULT_IMAGE
        
        # 调用原始的创建方法
        return self.create_training_job(
            name=name,
            logic_compute_group_id=compute_group_id,
            project_id=project_id,
            workspace_id=workspace_id,
            framework=framework,
            command=command,
            spec_id=spec_id,
            task_priority=task_priority,
            auto_fault_tolerance=auto_fault_tolerance,
            enable_notification=enable_notification,
            enable_troubleshoot=enable_troubleshoot,
            image=image,
            instance_count=instance_count,
            shm_gi=shm_gi,
            max_running_time_ms=max_running_time_ms,
            **kwargs
        )
    
    def create_training_job(self, 
                           name: str, 
                           logic_compute_group_id: str, 
                           project_id: str,
                           workspace_id: str,
                           framework: str,
                           command: str,
                           spec_id: str,
                           task_priority: int = DEFAULT_TASK_PRIORITY,
                           auto_fault_tolerance: bool = False,
                           enable_notification: bool = False,
                           enable_troubleshoot: bool = False,
                           image: str = "",
                           image_type: str = DEFAULT_IMAGE_TYPE,
                           instance_count: int = DEFAULT_INSTANCE_COUNT,
                           shm_gi: int = DEFAULT_SHM_SIZE,
                           max_running_time_ms: str = DEFAULT_MAX_RUNNING_TIME,
                           reserve_on_fail_ms: str = "0",
                           reserve_on_success_ms: str = "0",
                           tb_summary_path: str = "",
                           dataset_info: Optional[list] = None,
                           envs: Optional[list] = None) -> Dict[str, Any]:
        """创建分布式训练任务（原始方法）"""
        self._check_authentication()
        
        # 验证必需参数
        self._validate_required_params(
            name=name,
            logic_compute_group_id=logic_compute_group_id,
            project_id=project_id,
            workspace_id=workspace_id,
            framework=framework,
            command=command,
            spec_id=spec_id
        )
        
        # 验证数值参数
        if instance_count < 1:
            raise ValidationError("Instance count must be at least 1")
        if shm_gi < 1:
            raise ValidationError("Shared memory size must be at least 1")
        if task_priority < 1 or task_priority > 10:
            raise ValidationError("Task priority must be between 1 and 10")
        
        # 使用默认镜像（如果未提供）
        if not image:
            image = self.DEFAULT_IMAGE
        
        # 构建请求负载
        payload = {
            "name": name,
            "logic_compute_group_id": logic_compute_group_id,
            "project_id": project_id,
            "workspace_id": workspace_id,
            "framework": framework,
            "command": command,
            "task_priority": task_priority,
            "auto_fault_tolerance": auto_fault_tolerance,
            "enable_notification": enable_notification,
            "enable_troubleshoot": enable_troubleshoot,
            "max_running_time_ms": max_running_time_ms,
            "reserve_on_fail_ms": reserve_on_fail_ms,
            "reserve_on_success_ms": reserve_on_success_ms,
            "tb_summary_path": tb_summary_path,
            "framework_config": [{
                "image": image,
                "image_type": image_type,
                "instance_count": instance_count,
                "shm_gi": shm_gi,
                "spec_id": spec_id
            }],
            "dataset_info": dataset_info or [],
            "envs": envs or []
        }
        
        logger.debug("Creating training job with payload structure defined")
        
        try:
            result = self._make_request('POST', APIEndpoints.TRAIN_JOB_CREATE, payload)
            
            if result.get('code') == 0:
                logger.info(f"✅ Training job '{name}' created successfully.")
                if 'data' in result and 'job_id' in result['data']:
                    logger.info(f"🆔 Job ID: {result['data']['job_id']}")
                return result
            else:
                error_msg = result.get('message', 'Unknown error')
                raise JobCreationError(f"Failed to create training job: {error_msg}")
                
        except InspireAPIError as e:
            if "Failed to create training job" in str(e):
                raise
            raise JobCreationError(f"Training job creation request failed: {str(e)}")
    
    def get_job_detail(self, job_id: str) -> Dict[str, Any]:
        """获取训练任务详情"""
        self._check_authentication()
        self._validate_required_params(job_id=job_id)
        
        payload = {"job_id": job_id}
        
        result = self._make_request('POST', APIEndpoints.TRAIN_JOB_DETAIL, payload)
        
        if result.get('code') == 0:
            logger.info(f"📋 Retrieved details for job {job_id}")
            return result
        else:
            error_msg = result.get('message', 'Unknown error')
            raise InspireAPIError(f"Failed to get job details: {error_msg}")
    
    def stop_training_job(self, job_id: str) -> bool:
        """停止训练任务"""
        self._check_authentication()
        self._validate_required_params(job_id=job_id)
        
        payload = {"job_id": job_id}
        
        result = self._make_request('POST', APIEndpoints.TRAIN_JOB_STOP, payload)
        
        if result.get('code') == 0:
            logger.info(f"🛑 Training job {job_id} stopped successfully.")
            return True
        else:
            error_msg = result.get('message', 'Unknown error')
            raise InspireAPIError(f"Failed to stop training job: {error_msg}")
    
    def list_available_specs(self, logic_compute_group_id: str) -> Dict[str, Any]:
        """获取可用的规格列表"""
        self._check_authentication()
        self._validate_required_params(logic_compute_group_id=logic_compute_group_id)
        
        payload = {"logic_compute_group_id": logic_compute_group_id}
        
        result = self._make_request('POST', APIEndpoints.SPECS_LIST, payload)
        
        if result.get('code') == 0:
            logger.info("📊 Retrieved available specs successfully.")
            return result
        else:
            error_msg = result.get('message', 'Unknown error')
            raise InspireAPIError(f"Failed to get specs: {error_msg}")

    def list_cluster_nodes(self,
                          page_num: int = 1, 
                          page_size: int = 10,
                          resource_pool: Optional[str] = None) -> Dict[str, Any]:
        """获取集群节点列表"""
        self._check_authentication()
        
        if page_num < 1:
            raise ValidationError("Page number must be at least 1")
        if page_size < 1 or page_size > 100:
            raise ValidationError("Page size must be between 1 and 100")
        
        valid_pools = ['online', 'backup', 'fault', 'unknown']
        if resource_pool and resource_pool not in valid_pools:
            raise ValidationError(f"Resource pool must be one of: {valid_pools}")
        
        payload = {
            "page_num": page_num,
            "page_size": page_size
        }
        
        if resource_pool:
            payload["filter"] = {"resource_pool": resource_pool}
        
        result = self._make_request('POST', APIEndpoints.CLUSTER_NODES_LIST, payload)
        
        if result.get('code') == 0:
            node_count = len(result['data'].get('nodes', []))
            logger.info(f"🖥️  Retrieved {node_count} nodes successfully.")
            return result
        else:
            error_msg = result.get('message', 'Unknown error')
            raise InspireAPIError(f"Failed to get node list: {error_msg}")


def get_credentials() -> tuple[str, str]:
    """从环境变量获取凭证"""
    username = os.getenv('INSPIRE_USERNAME')
    password = os.getenv('INSPIRE_PASSWORD')
    
    if not username:
        raise ValidationError(
            "❌ Username not found. Please set INSPIRE_USERNAME environment variable.\n"
            "   Example: export INSPIRE_USERNAME='your_username'"
        )
    
    if not password:
        raise ValidationError(
            "❌ Password not found. Please set INSPIRE_PASSWORD environment variable.\n"
            "   Example: export INSPIRE_PASSWORD='your_password'"
        )
    
    return username, password


def main():
    """主函数，提供命令行接口"""
    parser = argparse.ArgumentParser(
        description='🚀 启智平台API智能控制工具',
        epilog='凭证通过环境变量提供: INSPIRE_USERNAME 和 INSPIRE_PASSWORD\n'
               '使用 --show-resources 查看所有可用资源配置',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    # 全局选项
    parser.add_argument('--debug', action='store_true', help='启用调试模式')
    parser.add_argument('--base-url', type=str, default="https://qz.sii.edu.cn", 
                       help='API基础URL (默认: https://qz.sii.edu.cn)')
    parser.add_argument('--show-resources', action='store_true', 
                       help='显示所有可用资源配置并退出')
    
    subparsers = parser.add_subparsers(dest='command', help='可用命令')
    
    # 智能创建训练任务
    create_parser = subparsers.add_parser('create', help='🎯 智能创建分布式训练任务')
    create_parser.add_argument('--name', required=True, type=str, help='训练任务名称')
    create_parser.add_argument('--start-command', required=True, type=str, help='启动命令')
    create_parser.add_argument('--resource', required=True, type=str, 
                              help='资源配置 (如: "H200", "4xH200", "8 H200", "H100")')
    create_parser.add_argument('--framework', type=str, default="pytorch", 
                              help='训练框架 (默认: pytorch)')
    create_parser.add_argument('--location', type=str, 
                              help='偏好的机房位置 (如: "1号", "2号", "3号")')
    create_parser.add_argument('--priority', type=int, default=8, 
                              help='任务优先级 1-10 (默认: 8)')
    create_parser.add_argument('--image', type=str, 
                              help='自定义镜像名称 (可选)')
    create_parser.add_argument('--instances', type=int, default=1, 
                              help='实例数量 (默认: 1)')
    create_parser.add_argument('--shm-size', type=int, default=40, 
                              help='共享内存大小(Gi) (默认: 40)')
    create_parser.add_argument('--max-time-hours', type=float, default=100.0, 
                              help='最大运行时间(小时) (默认: 100)')
    create_parser.add_argument('--project-id', type=str, 
                              help='项目ID (可选，使用默认值)')
    create_parser.add_argument('--workspace-id', type=str, 
                              help='工作空间ID (可选，使用默认值)')
    create_parser.add_argument('--auto-fault-tolerance', action='store_true', 
                              help='开启自动容错')
    create_parser.add_argument('--enable-notification', action='store_true', 
                              help='启用通知')
    create_parser.add_argument('--enable-troubleshoot', action='store_true', 
                              help='启用故障排除')
    
    # 查询任务详情
    detail_parser = subparsers.add_parser('detail', help='📋 查询训练任务详情')
    detail_parser.add_argument('--job-id', required=True, type=str, help='任务ID')
    
    # 停止训练任务
    stop_parser = subparsers.add_parser('stop', help='🛑 停止训练任务')
    stop_parser.add_argument('--job-id', required=True, type=str, help='任务ID')
    
    # 列出可用规格
    specs_parser = subparsers.add_parser('list-specs', help='📊 列出可用的计算规格')
    specs_parser.add_argument('--resource', type=str, 
                             help='资源类型 (如: "H200", "H100")，用于自动选择计算组')
    specs_parser.add_argument('--compute-group-id', type=str, 
                             help='指定计算资源组ID')
    
    # 列出集群节点
    list_parser = subparsers.add_parser('list-nodes', help='🖥️  列出集群节点')
    list_parser.add_argument('--page', type=int, default=1, help='页码 (默认: 1)')
    list_parser.add_argument('--size', type=int, default=10, help='每页数量 (默认: 10)')
    list_parser.add_argument('--pool', type=str, choices=['online', 'backup', 'fault', 'unknown'], 
                            help='资源池过滤')
    
    args = parser.parse_args()
    
    # 显示资源配置并退出
    if args.show_resources:
        resource_manager = ResourceManager()
        resource_manager.display_available_resources()
        return 0
    
    # 设置日志级别
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
        logger.debug("🐛 Debug mode enabled")
    
    try:
        # 从环境变量获取凭证
        username, password = get_credentials()
        
        # 创建API客户端
        config = InspireConfig(base_url=args.base_url)
        api = InspireAPI(config)
        
        # 认证
        logger.info("🔐 Authenticating with Inspire API...")
        api.authenticate(username, password)
        
        # 根据命令执行相应操作
        if args.command == 'create':
            # 转换小时为毫秒
            max_time_ms = str(int(args.max_time_hours * 3600 * 1000))
            
            result = api.create_training_job_smart(
                name=args.name,
                command=args.start_command,
                resource=args.resource,
                framework=args.framework,
                prefer_location=args.location,
                project_id=args.project_id,
                workspace_id=args.workspace_id,
                image=args.image,
                task_priority=args.priority,
                instance_count=args.instances,
                shm_gi=args.shm_size,
                max_running_time_ms=max_time_ms,
                auto_fault_tolerance=args.auto_fault_tolerance,
                enable_notification=args.enable_notification,
                enable_troubleshoot=args.enable_troubleshoot
            )
            
            print("\n✅ 创建结果:")
            print(json.dumps(result, indent=2, ensure_ascii=False))
        
        elif args.command == 'detail':
            result = api.get_job_detail(args.job_id)
            print("\n📋 任务详情:")
            print(json.dumps(result, indent=2, ensure_ascii=False))
        
        elif args.command == 'stop':
            api.stop_training_job(args.job_id)
            print("🛑 任务已停止")
        
        elif args.command == 'list-specs':
            # 智能选择计算组ID
            compute_group_id = args.compute_group_id
            if not compute_group_id and args.resource:
                try:
                    _, compute_group_id = api.resource_manager.get_recommended_config(args.resource)
                    logger.info(f"🎯 Auto-selected compute group ID: {compute_group_id}")
                except ValueError as e:
                    logger.error(f"❌ Resource parsing failed: {e}")
                    return 1
            
            if not compute_group_id:
                logger.error("❌ Please specify either --resource or --compute-group-id")
                return 1
            
            result = api.list_available_specs(compute_group_id)
            print("\n📊 可用规格:")
            print(json.dumps(result, indent=2, ensure_ascii=False))
        
        elif args.command == 'list-nodes':
            result = api.list_cluster_nodes(
                page_num=args.page,
                page_size=args.size,
                resource_pool=args.pool
            )
            print("\n🖥️  节点列表:")
            print(json.dumps(result, indent=2, ensure_ascii=False))
        
        else:
            parser.print_help()
            print("\n💡 提示: 使用 --show-resources 查看所有可用资源配置")
            return 1
        
        return 0
        
    except (ValidationError, AuthenticationError, JobCreationError, InspireAPIError) as e:
        logger.error(f"❌ Error: {str(e)}")
        return 1
    except KeyboardInterrupt:
        logger.info("⏹️  Operation cancelled by user")
        return 1
    except Exception as e:
        logger.error(f"💥 Unexpected error: {str(e)}")
        if args.debug:
            import traceback
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    exit(main())
