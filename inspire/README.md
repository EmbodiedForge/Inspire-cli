# 启智平台智能API控制工具（旧版脚本说明）

> 注意：本文件描述的是旧版脚本 `inspire_api_control.py` 的用法，不适用于当前的 `inspire` CLI（位于 `inspire/cli`）。如需使用当前 CLI，请参阅仓库根目录的 `README.md`。

这是一个增强版的启智平台API控制脚本，支持自然语言指定计算资源，大大简化了用户使用体验。

## ✨ 新功能特性

### 🎯 智能资源匹配
- **自然语言指定**: 支持 "H200", "4xH200", "8 H200", "H100" 等自然表达
- **自动配置**: 自动匹配对应的 spec-id 和 compute-group-id
- **机房选择**: 支持指定偏好的机房位置（1号/2号/3号机房）

### 📊 资源配置一览
```bash
# 查看所有可用资源配置
python inspire_api_control.py --show-resources
```

### 🚀 简化的使用方式
```bash
# 旧方式（需要手动指定复杂ID）
python old_script.py create --name "my-job" --start-command "python train.py" \
  --spec-id "4dd0e854-e2a4-4253-95e6-64c13f0b5117" \
  --compute-group-id "lcg-303ac8c6-aa19-4284-af03-2296592326e5"

# 新方式（自然语言指定）
python inspire_api_control.py create --name "my-job" --start-command "python train.py" \
  --resource "H200"
```

## 📋 使用指南

### 环境准备
```bash
# 设置认证信息
export INSPIRE_USERNAME='your_username'
export INSPIRE_PASSWORD='your_password'

# 安装依赖（如需要）
pip install requests
```

### 🎯 创建训练任务（智能模式）

#### 基础用法
```bash
# 使用单个H200 GPU
python inspire_api_control.py create \
  --name "my-pytorch-training" \
  --start-command "python train.py --epochs 100" \
  --resource "H200"

# 使用4个H200 GPU
python inspire_api_control.py create \
  --name "distributed-training" \
  --start-command "torchrun --nproc_per_node=4 train.py" \
  --resource "4xH200"

# 使用8个H200 GPU
python inspire_api_control.py create \
  --name "large-scale-training" \
  --start-command "torchrun --nproc_per_node=8 train.py" \
  --resource "8 H200"

# 使用H100 GPU
python inspire_api_control.py create \
  --name "h100-training" \
  --start-command "python train.py" \
  --resource "H100"
```

#### 高级选项
```bash
python inspire_api_control.py create \
  --name "advanced-training" \
  --start-command "python train.py --lr 0.001" \
  --resource "4xH200" \
  --location "2号" \
  --framework "tensorflow" \
  --priority 9 \
  --max-time-hours 48 \
  --shm-size 64 \
  --auto-fault-tolerance \
  --enable-notification
```

### 📋 任务管理

#### 查询任务详情
```bash
python inspire_api_control.py detail --job-id "your-job-id"
```

#### 停止任务
```bash
python inspire_api_control.py stop --job-id "your-job-id"
```

### 📊 资源查询

#### 查看可用规格
```bash
# 自动选择计算组（推荐）
python inspire_api_control.py list-specs --resource "H200"

# 手动指定计算组
python inspire_api_control.py list-specs --compute-group-id "lcg-303ac8c6-aa19-4284-af03-2296592326e5"
```

#### 查看集群节点
```bash
python inspire_api_control.py list-nodes --page 1 --size 20
```

## 🎨 支持的资源表达方式

### GPU数量表达
- `H200` - 1个H200 GPU
- `4xH200` - 4个H200 GPU
- `4 H200` - 4个H200 GPU（空格分隔）
- `H200x4` - 4个H200 GPU
- `8xH200` - 8个H200 GPU
- `H100` - 1个H100 GPU

### 机房位置偏好
- `--location "1号"` - 偏好1号机房
- `--location "2号"` - 偏好2号机房
- `--location "3号"` - 偏好3号机房

## 🔧 配置参数说明

### 必需参数
- `--name`: 训练任务名称
- `--start-command`: 启动命令
- `--resource`: 资源配置（如 "H200", "4xH200"）

### 可选参数
- `--framework`: 训练框架（默认: pytorch）
- `--location`: 偏好机房位置
- `--priority`: 任务优先级 1-10（默认: 8）
- `--max-time-hours`: 最大运行时间（小时，默认: 100）
- `--instances`: 实例数量（默认: 1）
- `--shm-size`: 共享内存大小（Gi，默认: 40）
- `--image`: 自定义镜像名称
- `--project-id`: 项目ID（可选）
- `--workspace-id`: 工作空间ID（可选）

### 功能开关
- `--auto-fault-tolerance`: 开启自动容错
- `--enable-notification`: 启用通知
- `--enable-troubleshoot`: 启用故障排除
- `--debug`: 启用调试模式

## 📊 资源配置映射表

| 资源描述 | GPU配置 | CPU/内存 | Spec ID |
|---------|---------|----------|---------|
| H200, 1xH200 | 1 × H200 (141GB) | 15核/200GB | 4dd0e854-e2a4-4253-95e6-64c13f0b5117 |
| 4xH200 | 4 × H200 (141GB) | 60核/800GB | 45ab2351-fc8a-4d50-a30b-b39a5306c906 |
| 8xH200 | 8 × H200 (141GB) | 120核/1600GB | b618f5cb-c119-4422-937e-f39131853076 |

| 计算类型 | 位置 | Compute Group ID |
|---------|-----|------------------|
| H100 | CUDA 12.8版本 | lcg-79b2ad0e-a375-43f3-a0b1-b4ce79710fd7 |
| H200 | 1号机房 | lcg-df089db8-817a-4aa8-a164-eb1a32948564 |
| H200 | 2号机房 | lcg-303ac8c6-aa19-4284-af03-2296592326e5 |
| H200 | 3号机房 | lcg-a91ad10b-415d-4abd-8170-828a2feae5d2 |

## 🚨 常见问题

### Q: 如何查看所有可用资源？
```bash
python inspire_api_control.py --show-resources
```

### Q: 资源表达不被识别怎么办？
脚本会提示可用的资源配置，请参考错误消息中的建议。

### Q: 如何指定特定机房？
使用 `--location` 参数：
```bash
--location "2号"  # 偏好2号机房
```

### Q: 如何使用自定义镜像？
```bash
--image "your-custom-image:tag"
```

### Q: 认证失败怎么办？
检查环境变量设置：
```bash
echo $INSPIRE_USERNAME
echo $INSPIRE_PASSWORD
```

## 🔄 与原版本的兼容性

本脚本完全兼容原始API，所有原有功能都保留。如果需要使用原始的详细参数模式，仍然可以直接调用 `create_training_job()` 方法。

## 🎯 最佳实践

1. **资源选择**: 根据模型大小选择合适的GPU数量
2. **机房选择**: 根据数据位置选择就近机房
3. **任务命名**: 使用描述性的任务名称便于管理
4. **时间设置**: 合理设置最大运行时间避免资源浪费
5. **监控**: 开启通知功能及时了解任务状态

## 📝 示例脚本

### 单GPU训练示例
```bash
python inspire_api_control.py create \
  --name "bert-fine-tuning" \
  --start-command "python train_bert.py --model bert-base --dataset squad" \
  --resource "H200" \
  --max-time-hours 24
```

### 多GPU分布式训练示例
```bash
python inspire_api_control.py create \
  --name "llama-training-4gpu" \
  --start-command "torchrun --nproc_per_node=4 train_llama.py --config config.yaml" \
  --resource "4xH200" \
  --location "2号" \
  --max-time-hours 72 \
  --shm-size 80 \
  --auto-fault-tolerance
```
