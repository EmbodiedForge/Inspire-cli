# Changelog

<!-- version list -->

## v0.7.4 (2026-11-25)

### Bug Fixes

- **auth**: Renew verified QZ web sessions
  ([`7ce5be6`](https://codeberg.org/cyteena/inspire-cli/commit/7ce5be6727d1ba83456c875adf094aef821dd822))

- **dataset**: Include the account in data-square login hints
  ([`410d0cc`](https://codeberg.org/cyteena/inspire-cli/commit/410d0cccd1be863e53eba8b14bae25bc970ceee3))


## v0.7.3 (2026-11-20)

### Bug Fixes

- **notebook**: Describe --all as a user-scoped no-op
  ([`4e9c910`](https://codeberg.org/cyteena/inspire-cli/commit/4e9c910db458cfc1266da67ed24dc8018b0902c5))

- **notebook**: Reject unsupported --all scope
  ([`aa7bbdd`](https://codeberg.org/cyteena/inspire-cli/commit/aa7bbdd44850c2b9b3c6f9b8533f02a0d58670d2))


## v0.7.2 (2026-11-20)

### Bug Fixes

- **capacity**: Count only schedulable nodes
  ([`1f30793`](https://codeberg.org/cyteena/inspire-cli/commit/1f307939944b77da97680dd13a185678c2975f8e))

- **job**: Describe list as training-job history
  ([`0b1767f`](https://codeberg.org/cyteena/inspire-cli/commit/0b1767feba793e1121e0cc7c9649b089bf4380c6))

- **web**: Route dimension actions to workspace
  ([`d394323`](https://codeberg.org/cyteena/inspire-cli/commit/d3943236fd772862fdd11628322bf4605ddf4235))


## v0.7.1 (2026-11-19)

### Bug Fixes

- **capacity**: Restore advisory low-priority usage
  ([`1707c3a`](https://codeberg.org/cyteena/inspire-cli/commit/1707c3a09c55d1c12fdd3f39b3e4a8e56489ac3a))


## v0.7.0 (2026-11-18)

### Refactoring

- Retire aggregate capacity fallback
  ([`84fded3`](https://codeberg.org/cyteena/inspire-cli/commit/84fded383dba48614a69a4d2bca906c0618e56d9))


## v0.6.14 (2026-11-18)

### Bug Fixes

- **skills**: Separate node capacity from aggregate free GPUs
  ([`9a7489c`](https://codeberg.org/cyteena/inspire-cli/commit/9a7489c626e99feb59bcf60cc86cb76c186e4d55))


## v0.6.13 (2026-11-18)

### Bug Fixes

- **resources**: Read node dimensions from the workspace service
  ([`fe368c4`](https://codeberg.org/cyteena/inspire-cli/commit/fe368c40ecc2b9bf00e73a53e37caf2c049cf589))


## v0.6.12 (2026-11-18)

### Bug Fixes

- **cli**: Report project quota exhaustion in pending hints
  ([`e174a51`](https://codeberg.org/cyteena/inspire-cli/commit/e174a51113073795978380a8f440408dff14b63f))


## v0.6.11 (2026-11-18)

### Bug Fixes

- **cli**: Add agent-friendly scheduling hints and shortcut guard
  ([`3182c02`](https://codeberg.org/cyteena/inspire-cli/commit/3182c028e344259a6969b224b29ba9b9532528fa))


## v0.6.10 (2026-11-18)

### Refactoring

- **config**: Retire unused log_pattern setting
  ([`cdb7e1d`](https://codeberg.org/cyteena/inspire-cli/commit/cdb7e1d49755bc6e8da83a775a3892f0836e5526))


## v0.6.9 (2026-11-18)

### Refactoring

- **job**: Retire local job log file
  ([`fc3343e`](https://codeberg.org/cyteena/inspire-cli/commit/fc3343ea14277fd89b3a65db2251d4e8ccfbdff0))


## v0.6.8 (2026-11-18)

### Features

- **job**: Add --shm and always send shm_gi
  ([`81b669b`](https://codeberg.org/cyteena/inspire-cli/commit/81b669bdb3f87f362446d30861ceaf0f4e4fc5d1))


## v0.6.7 (2026-11-18)

### Bug Fixes

- **auth**: Preserve cookie flags when refreshing SSO state
  ([`c2f329c`](https://codeberg.org/cyteena/inspire-cli/commit/c2f329c7f15d98300ff189e3e87adba12a191d02))


## v0.6.6 (2026-11-18)

### Bug Fixes

- **auth**: Refresh stale qz session cookies through SSO
  ([`a934fee`](https://codeberg.org/cyteena/inspire-cli/commit/a934fee1d517d3e8c1f3a43754fcbfe1311322c3))


## v0.6.5 (2026-11-17)

### Bug Fixes

- **packaging**: Point project URLs at codeberg
  ([#143](https://codeberg.org/cyteena/inspire-cli/pulls/143),
  [`3e03fb1`](https://codeberg.org/cyteena/inspire-cli/commit/3e03fb1c3a879b424e12f0017dbcdf93ff0650f7))


## v0.6.4 (2026-11-17)

### Bug Fixes

- **cli**: Clamp list page size and add mutation coverage
  ([#140](https://codeberg.org/cyteena/inspire-cli/pulls/140),
  [`0689fce`](https://codeberg.org/cyteena/inspire-cli/commit/0689fce0460c58e72b35988794f83f6e2fc74a4a))

- **cli**: List every visible job without owner filtering
  ([#142](https://codeberg.org/cyteena/inspire-cli/pulls/142),
  [`2b75679`](https://codeberg.org/cyteena/inspire-cli/commit/2b7567978c107d3ad83be3152e8ef78d7711314c))

- **cli**: Treat job create failures as terminal
  ([#141](https://codeberg.org/cyteena/inspire-cli/pulls/141),
  [`315a933`](https://codeberg.org/cyteena/inspire-cli/commit/315a933e5862fed1622ebe352909b8c845e987c6))


## v0.6.3 (2026-11-17)

### Bug Fixes

- **cli**: Harden listing and lifecycle contracts
  ([#139](https://codeberg.org/cyteena/inspire-cli/pulls/139),
  [`f20030b`](https://codeberg.org/cyteena/inspire-cli/commit/f20030b8f27fd2a867fac208833fcea938ad08e8))

- **cli**: Repair job policy findings ([#134](https://codeberg.org/cyteena/inspire-cli/pulls/134),
  [`540f600`](https://codeberg.org/cyteena/inspire-cli/commit/540f6002c7396c1569c3d15e51f54301b84ca8d3))


## v0.6.2 (2026-11-17)

### Bug Fixes

- **cli**: Correct cross-surface command contracts
  ([#129](https://codeberg.org/cyteena/inspire-cli/pulls/129),
  [`1c407e1`](https://codeberg.org/cyteena/inspire-cli/commit/1c407e180cd6e0b9fa20281ce09f52bf409cbe9e))

- **cli**: Repair api examples and browser guidance
  ([#128](https://codeberg.org/cyteena/inspire-cli/pulls/128),
  [`8a39d41`](https://codeberg.org/cyteena/inspire-cli/commit/8a39d41acf80c55e89cc501a39edcc51981f6e86))


## v0.6.1 (2026-11-16)

### Bug Fixes

- **cli**: Scope job planning to selected offers
  ([#121](https://codeberg.org/cyteena/inspire-cli/pulls/121),
  [`054b277`](https://codeberg.org/cyteena/inspire-cli/commit/054b277a23943cd2385b36cbd13b2c7673e5af43))

### Features

- **cli**: Add authenticated browser eval escape hatch
  ([#122](https://codeberg.org/cyteena/inspire-cli/pulls/122),
  [`4556f00`](https://codeberg.org/cyteena/inspire-cli/commit/4556f00dbc688f04c8c8bf67b9e9ca05d8994f24))

- **cli**: Unify console access under api eval
  ([#123](https://codeberg.org/cyteena/inspire-cli/pulls/123),
  [`c56e322`](https://codeberg.org/cyteena/inspire-cli/commit/c56e32238c7eb4e07730bcacfee32094198bfed5))


## v0.6.0 (2026-11-16)

### Bug Fixes

- **api**: Notebook metrics reach their Actions, and the window fits the interval
  ([`a73f438`](https://codeberg.org/cyteena/inspire-cli/commit/a73f438520ea59c4aa94e8fbd719a0891e5543df))

- **api**: The API-key commands call the live Actions, not retired v1 paths
  ([`13b058c`](https://codeberg.org/cyteena/inspire-cli/commit/13b058cd12e48a42be21bf3cb1131b1aa62ab949))

- **api**: The job log surfaces reach their Actions, and templates stop hiding
  ([`c807c4a`](https://codeberg.org/cyteena/inspire-cli/commit/c807c4a547eb4366e057f684b9309494b3e89db2))

- **cli**: Show low-priority GPU usage in resource views
  ([#114](https://codeberg.org/cyteena/inspire-cli/pulls/114),
  [`4d81ddc`](https://codeberg.org/cyteena/inspire-cli/commit/4d81ddcbe65b6e785daa1551feac4bcd79ecc080))

- **cli**: Tell the truth about what an execd error already retried
  ([`b49b9f8`](https://codeberg.org/cyteena/inspire-cli/commit/b49b9f84d4b0cd4393ea52253eb472ebe3fcb657))

- **e2e**: The live core lane stops asking resources list for a flag that was deleted
  ([`46f44aa`](https://codeberg.org/cyteena/inspire-cli/commit/46f44aa1f8dcf7480937081c8f008f9251585c01))

- **e2e**: The sweep's refresh knob is declared where the env contract owns it
  ([`6c30b3a`](https://codeberg.org/cyteena/inspire-cli/commit/6c30b3a791ec7de25c865918b93512927cad00c6))

- **job**: A fair workspace's priority ceiling is applied in one submit, not searched for
  ([`1e9330d`](https://codeberg.org/cyteena/inspire-cli/commit/1e9330d382c826ae20ebfaead3b82849394ee5f4))

- **job**: Carry main's exec guarantees across the control-plane rewrite
  ([`44066ae`](https://codeberg.org/cyteena/inspire-cli/commit/44066ae6e4aafdaddb60199b3d9d8eda786eae78))

- **job**: Fair-workspace submits find a priority the platform takes, and name the one it took
  ([`5c17d95`](https://codeberg.org/cyteena/inspire-cli/commit/5c17d95e5d7d995f7417e45defafe79a3a1e5aff))

- **job**: Quota tells the reader the priority ceiling the space declares, not only what each offer
  declares
  ([`42198bf`](https://codeberg.org/cyteena/inspire-cli/commit/42198bf6801b49a85d3a5eab03ab7b1b9d383514))

- **job**: Stabilize execd transport and session state
  ([`0ce324c`](https://codeberg.org/cyteena/inspire-cli/commit/0ce324c2c845c70259dd530278bdc65f5714bbbe))

- **job**: Stop a loop's terminating test from becoming the exec exit code
  ([`88a818d`](https://codeberg.org/cyteena/inspire-cli/commit/88a818d39af96bbd91427551f21110567a338cfc))

- **nightly**: Stop pushing the nightly verdict to Feishu
  ([`48e479d`](https://codeberg.org/cyteena/inspire-cli/commit/48e479d1dd0430a856aa6520abf34da340d08e80))

- **notebook**: A spent --timeout names its own budget, and a dead channel its own words
  ([`786ce80`](https://codeberg.org/cyteena/inspire-cli/commit/786ce8015157c789245619f21e12820be8d28bef))

- **notebook**: An exhausted watch answers 124 with a cursor, and a bare exec follows its exec to
  the end
  ([`2c0a2d0`](https://codeberg.org/cyteena/inspire-cli/commit/2c0a2d003abde2583faa49ce6938eff8ce39ed7d))

- **notebook**: Read which groups can run one before choosing or accepting one
  ([`f1ca348`](https://codeberg.org/cyteena/inspire-cli/commit/f1ca34857d33c46f512bc83c20a758d1122bb456))

- **notebook**: Top answers a CPU-only pod instead of failing on nvidia-smi
  ([`301c65d`](https://codeberg.org/cyteena/inspire-cli/commit/301c65dc0a5e0bbc6bada1c94528d70a9670dbb3))

- **resources**: Stop offering the removed --all flag, in the live suite and in five hints
  ([`36c3d98`](https://codeberg.org/cyteena/inspire-cli/commit/36c3d980ac8d0e73ea808e658664d396830428a3))

- **review**: The local nightly supplies the proxy its cron environment does not have
  ([`80c1993`](https://codeberg.org/cyteena/inspire-cli/commit/80c1993f882f523cf7e5c0184ed0ba95aae18bb1))

- **skills**: The agent-facing skills stop teaching commands and flags that no longer exist
  ([`abb86ce`](https://codeberg.org/cyteena/inspire-cli/commit/abb86ce93dadb90246d4ac1d0f1e88038d45b0f9))

- **sweep**: A row may only claim what it was actually given
  ([`1aaa1a2`](https://codeberg.org/cyteena/inspire-cli/commit/1aaa1a29d8eb89df05bf07dc7809599b49f0bc38))

- **sweep**: The regression report renders from the keys the diff emits, and the refresh knob
  survives conftest
  ([`3cb1f2d`](https://codeberg.org/cyteena/inspire-cli/commit/3cb1f2d0682764975404406e7551e69f690a4aaf))

- **sweep**: Track the read-surface judge, and fix the two script bugs it found
  ([`e2a2e3a`](https://codeberg.org/cyteena/inspire-cli/commit/e2a2e3a2987f8281edea17cdf764dabe473e202b))

### Features

- **cli**: Read the container's own cgroup truth in notebook limits
  ([`634bdf4`](https://codeberg.org/cyteena/inspire-cli/commit/634bdf49fdb1da13354b541319f360999c38c125))

- **job**: Move exec control plane to pod-side execd
  ([`ea523f0`](https://codeberg.org/cyteena/inspire-cli/commit/ea523f0f8e99ca44991424bbe994a77de309d540))

- **job-exec**: A bare exec is watched to its own verdict, and the resume watch reads on the same
  terms
  ([`7c5585a`](https://codeberg.org/cyteena/inspire-cli/commit/7c5585aaa168fa329c02d9df728f5b104cf7a8f5))

- **job-exec**: A plain exec keeps reading when the platform kills its channel, and says so once
  ([`04b613c`](https://codeberg.org/cyteena/inspire-cli/commit/04b613cc477de64a2466c5b4359973ce3f171d5d))

- **notebook**: A bare exec now runs off the terminal and streams its record back, so a dead channel
  costs a reconnect
  ([`60db298`](https://codeberg.org/cyteena/inspire-cli/commit/60db298f9c59f1a25ede045fa91d642615e89770))

- **notebook**: Merge top, limits and metrics into one resource view
  ([`ca91ef5`](https://codeberg.org/cyteena/inspire-cli/commit/ca91ef548043d189b8c9a983b32a539814405db7))

- **notebook**: Tell a dead terminal channel from an expired timeout, and add exec --detach/--attach
  ([`cbf98ca`](https://codeberg.org/cyteena/inspire-cli/commit/cbf98cad3d4ae9854e6927dd2eff4f982d55bcb8))

- **sweep**: A ceiling and a receipt for a run that spends cluster time
  ([`e908ab1`](https://codeberg.org/cyteena/inspire-cli/commit/e908ab1596c615b68f16fdebd119c14828dfc129))

- **sweep**: A lane that sweeps every command, and cannot report a quiet night as healthy
  ([`86f6140`](https://codeberg.org/cyteena/inspire-cli/commit/86f61405e9d98b774c774eec0b674de475894fa4))

- **sweep**: One sweep at a time
  ([`762b4c5`](https://codeberg.org/cyteena/inspire-cli/commit/762b4c53cce6bcb94b3e927bb5c95d4f2938284f))


## v0.5.8 (2026-11-05)

### Bug Fixes

- **cli**: Align auto-selection with workload priority
  ([`29acbed`](https://codeberg.org/cyteena/inspire-cli/commit/29acbed9521ef1edec9beb342765c1efeff873ea))

- **cli**: Make job exec failures diagnosable
  ([`043d9b7`](https://codeberg.org/cyteena/inspire-cli/commit/043d9b78e71c9e10d0f9445b69b8b6e24e2c2ca0))

- **cli**: Preserve resource specs and priority-aware selection
  ([`24d015b`](https://codeberg.org/cyteena/inspire-cli/commit/24d015bb406b6c641605e14d0f640a85690b5384))

- **cli**: State the workspace scope and list the front-only --file
  ([`96842ee`](https://codeberg.org/cyteena/inspire-cli/commit/96842eec4728285f8ca72f461dd6bb28c667e83e))

- **tests**: Assert the temp directory's shape, not a literal /tmp
  ([`635e724`](https://codeberg.org/cyteena/inspire-cli/commit/635e724e768feb2acd97692a3ab4c30a0ceae4b7))

### Features

- **cli**: Take workspace identity from the platform, not from invented aliases
  ([`3b3ae37`](https://codeberg.org/cyteena/inspire-cli/commit/3b3ae3785975db61e48a238a37cdd49cba88e312))

### Refactoring

- Clear the mypy debt and drop the lying tunnel internet flags
  ([`25d5d55`](https://codeberg.org/cyteena/inspire-cli/commit/25d5d55fe34254b67023d5ef2f7c25c2c7a212b4))


## v0.5.7 (2026-10-30)

### Features

- **cli**: Discover current console API contracts
  ([#89](https://codeberg.org/cyteena/inspire-cli/pulls/89),
  [`5749142`](https://codeberg.org/cyteena/inspire-cli/commit/5749142baefdee1a40336a1f59f9d01e34a9c896))


## v0.5.6 (2026-10-28)

### Bug Fixes

- **cli**: Expose workspace scheduling policies
  ([#87](https://codeberg.org/cyteena/inspire-cli/pulls/87),
  [`161d94c`](https://codeberg.org/cyteena/inspire-cli/commit/161d94c461a220006bba7c7d69b3965bffaeea38))

- **image**: Handle empty registry robot credentials
  ([#88](https://codeberg.org/cyteena/inspire-cli/pulls/88),
  [`da94409`](https://codeberg.org/cyteena/inspire-cli/commit/da94409718126c541fa9f0a18d2f85849d32a37e))


## v0.5.5 (2026-10-27)

### Bug Fixes

- **image**: Migrate custom image operations to v2
  ([`d634f46`](https://codeberg.org/cyteena/inspire-cli/commit/d634f46d68710c5a89f3bbdc5032f94f33ee6d0d))


## v0.5.4 (2026-10-19)

### Bug Fixes

- **auth**: Verify web sessions through the v2 Action call
  ([#85](https://codeberg.org/cyteena/inspire-cli/pulls/85),
  [`f2dac87`](https://codeberg.org/cyteena/inspire-cli/commit/f2dac875ce706fc1965a8f22986fb1fb3c27d881))

- **cli**: Place jobs from aggregate counts when node probing is denied
  ([#86](https://codeberg.org/cyteena/inspire-cli/pulls/86),
  [`57d4b83`](https://codeberg.org/cyteena/inspire-cli/commit/57d4b831687b908dd546d26361cc3adc6ab363e0))


## v0.5.3 (2026-10-14)

### Features

- **dataset**: Browse the data square and mount datasets on jobs/notebooks
  ([#79](https://codeberg.org/cyteena/inspire-cli/pulls/79),
  [`0d16382`](https://codeberg.org/cyteena/inspire-cli/commit/0d1638258077f65c55ecde6438b6d49f7722ab44))


## v0.5.2 (2026-10-10)

### Bug Fixes

- **cli**: Cap workload priority at the project's own priority
  ([`8f59ed0`](https://codeberg.org/cyteena/inspire-cli/commit/8f59ed04451b28cb13cd479961d8c322fc924657))


## v0.5.1 (2026-10-09)

### Bug Fixes

- **bridge**: Report a displacement only when one happened
  ([`a8929e9`](https://codeberg.org/cyteena/inspire-cli/commit/a8929e9e08a72097c8a2870ea90ca59a5cae35f0))

### Features

- **bridge**: Non-destructive tree sync with rsync/tar engines
  ([#33](https://codeberg.org/cyteena/inspire-cli/pulls/33),
  [`9651c94`](https://codeberg.org/cyteena/inspire-cli/commit/9651c94ddd4043cc4d41a6898aebb416415aea1f))

- **bridge**: Sync trees over the Jupyter transport with the tar engine
  ([`0afd59f`](https://codeberg.org/cyteena/inspire-cli/commit/0afd59fb22f6b12bdc8966c4b6179020c876a146))

- **job**: Run a batch of exec commands down one session
  ([`df91fdb`](https://codeberg.org/cyteena/inspire-cli/commit/df91fdb1bbe2b5a995e50c836751965068f197f3))

- **job**: Say when a keep-alive round gives its card back
  ([`76f23ec`](https://codeberg.org/cyteena/inspire-cli/commit/76f23ececb405274e2317013ae1a9100702b6a87))

- **job**: Stateful exec through a persistent pod-side shell
  ([#55](https://codeberg.org/cyteena/inspire-cli/pulls/55),
  [`9e1104e`](https://codeberg.org/cyteena/inspire-cli/commit/9e1104e5549388077ca4a9c9a2e852e1c7c532ea))

- **sync**: --dest and --local overrides on inspire sync
  ([#34](https://codeberg.org/cyteena/inspire-cli/pulls/34),
  [`05deee8`](https://codeberg.org/cyteena/inspire-cli/commit/05deee823a59ad34c1a3e70b9b82f7c70b17d8a3))


## v0.5.0 (2026-10-07)

### Bug Fixes

- **ci**: Track gen_e2e_env_doc.py so the release gate finds it
  ([`3f0ddba`](https://codeberg.org/cyteena/inspire-cli/commit/3f0ddbabcf1f81febfc4f227d467e4ce18522847))

- **cli**: Empty the empty-help debt; every command now explains itself
  ([`96b0fc8`](https://codeberg.org/cyteena/inspire-cli/commit/96b0fc82ef05085fa73a741f81034cbbf5d1eae1))

- **cli**: Every high-risk-write command now gates on --yes (#76 contract)
  ([`ffbf2cc`](https://codeberg.org/cyteena/inspire-cli/commit/ffbf2cc78b76fb26db31887efd8fc9ec7cd2959e))

- **cli**: Honest exec stderr, and instance phase age (#51 #61)
  ([`06c5981`](https://codeberg.org/cyteena/inspire-cli/commit/06c5981dd0da94e38eb978ebdba3bd106b998a1b))

- **cli**: Notebook exec failures publish the remote exit code instead of hiding it in prose
  ([`e96678d`](https://codeberg.org/cyteena/inspire-cli/commit/e96678d150b6e681fcbf7b66bb12ae122d9b73f2))

- **cli**: Restore the help text 10 commands had lost, and assert it stays restored
  ([`77b4766`](https://codeberg.org/cyteena/inspire-cli/commit/77b476636d1e636463be492f80cc540d3a6ab934))

- **cli**: Stop command groups from shadowing their own submodules
  ([`d02fb00`](https://codeberg.org/cyteena/inspire-cli/commit/d02fb00bc798714a30f2719e8a7208836aed834b))

- **cli**: Stop reporting failures as success across exec/tunnel/notebook/model paths (#66 #67)
  ([`1648639`](https://codeberg.org/cyteena/inspire-cli/commit/164863911460ea058f8a0ec4f58a5a3f5e48c2f1))

- **cli**: Tell users what the pass-through shortcuts actually accept
  ([`158556f`](https://codeberg.org/cyteena/inspire-cli/commit/158556f0904d5de39e684f2ed5e4da6a729dd7fd))

- **cli**: Wait and follow report BLIND instead of a quiet job
  ([#59](https://codeberg.org/cyteena/inspire-cli/pulls/59),
  [`b4bfb48`](https://codeberg.org/cyteena/inspire-cli/commit/b4bfb48bd18c88cdf22e94490c41f0b41fef7c56))

- **config**: Wrap config show JSON output in the standard envelope
  ([`f7380a6`](https://codeberg.org/cyteena/inspire-cli/commit/f7380a6a5155d3c122b4278f40459e875c7cbf1c))

- **config,notebook**: Atomic comment-preserving writes and download integrity (#68 #69)
  ([`655e8b7`](https://codeberg.org/cyteena/inspire-cli/commit/655e8b77769c584943d59cb18be7b6ab00292d8a))

- **exec**: A second cancel against an unreaped-zombie group says not-running
  ([`82d896e`](https://codeberg.org/cyteena/inspire-cli/commit/82d896e209608f424d19cf3209e35731e9ba6722))

- **exec**: Admit non-leader group members as cancel targets and liveness witnesses
  ([`85523bd`](https://codeberg.org/cyteena/inspire-cli/commit/85523bd7dc05d88c830c9890ba407f04aef248e1))

- **exec**: Count a TERM-trapped group mate when the leader sits an unreaped zombie
  ([`e8e47e6`](https://codeberg.org/cyteena/inspire-cli/commit/e8e47e6ae337757a588f1c1fdb147280726f3003))

- **exec**: Faithful pipeline exit codes, a 1 MiB tail-keeping output ceiling, and private redacted
  records (#52 #53 #56)
  ([`42249d9`](https://codeberg.org/cyteena/inspire-cli/commit/42249d9fb34fcd7c698ec8274bb726b9f774403d))

- **exec**: The three exec verbs agree on what the exit code is called
  ([`f427c39`](https://codeberg.org/cyteena/inspire-cli/commit/f427c39721c6940c9fc37ae6ca22ffbc3c093cab))

- **image**: Deduplicate --source all once visibility pins the views onto the same records
  ([`f9d0461`](https://codeberg.org/cyteena/inspire-cli/commit/f9d046143194c3bb380053e3acad1a2e38bc7a1a))

- **image**: Refuse a notebook name before /mirror/save can misanswer with "记录不存在"
  ([`2dbb2c0`](https://codeberg.org/cyteena/inspire-cli/commit/2dbb2c0b19092edd26e901d534074875202124c7))

- **image**: Reject a tag inside --name locally, and record two field-measured limits
  ([`884e0df`](https://codeberg.org/cyteena/inspire-cli/commit/884e0df4420af80264bb04d64017e68065069ee3))

- **image**: Resolve the save target with the same lookup the rest of the CLI uses
  ([`61e8d20`](https://codeberg.org/cyteena/inspire-cli/commit/61e8d20dc50b833bb5fe0771ec30139fbd5be37e))

- **image**: Say which view was searched when a list comes back empty
  ([`d8d4980`](https://codeberg.org/cyteena/inspire-cli/commit/d8d498087511401f93268ada50c48bb1651fa792))

- **image**: Send an explicit visibility on save, defaulting to personal
  ([`a6a7121`](https://codeberg.org/cyteena/inspire-cli/commit/a6a7121b4d0a6deabad25c3366eef437b8b29feb))

- **image**: Stop the origin column from being read as visibility
  ([`a4012ca`](https://codeberg.org/cyteena/inspire-cli/commit/a4012ca3c97b3cb19e111ad04152d5a387704711))

- **init**: Stop silent writeback damage in the discover flow
  ([#74](https://codeberg.org/cyteena/inspire-cli/pulls/74),
  [`fb3a682`](https://codeberg.org/cyteena/inspire-cli/commit/fb3a682461e2706cb9b91f20d3e770258e7c28ba))

- **job**: --active drops succeeded but keeps finished-but-reserving rounds
  ([#77](https://codeberg.org/cyteena/inspire-cli/pulls/77),
  [`994221d`](https://codeberg.org/cyteena/inspire-cli/commit/994221d0fcc15ce749bdfe49d1734fae7bdc904d))

- **job**: `job metrics --json` stops returning the wire payload verbatim
  ([`1eae81f`](https://codeberg.org/cyteena/inspire-cli/commit/1eae81f48b63b98b135f0f53f63f60cf527fd0d6))

- **job**: `job upload` speaks JSON, and records precisely what has been proven
  ([`7b3eb5d`](https://codeberg.org/cyteena/inspire-cli/commit/7b3eb5d1ca3f8cbe90ef8e2c5bd00929427f172d))

- **job**: A merged exec channel states its stream model instead of publishing an empty stderr
  ([`31aa1a5`](https://codeberg.org/cyteena/inspire-cli/commit/31aa1a5ebce913f07df5ff7dfe815c0a43b46808))

- **job**: A positive instance state outranks a scheduling event, and record the account shape
  ([`6bdafec`](https://codeberg.org/cyteena/inspire-cli/commit/6bdafeca1ed93fc61a569098462d06b9c959c1e7))

- **job**: Account for retry rounds in `job status`
  ([`76ae9fe`](https://codeberg.org/cyteena/inspire-cli/commit/76ae9fef7c4468fb48ed3b1be018d74479516360))

- **job**: Bound --priority and name both numbers when it gets capped
  ([`33758dd`](https://codeberg.org/cyteena/inspire-cli/commit/33758ddab65e793639810ada9df4dfc24a1fa6d3))

- **job**: Classify a dropped exec channel during detach/observe, and reject empty exec ids
  ([`47be7b7`](https://codeberg.org/cyteena/inspire-cli/commit/47be7b77b1f57e8ef3a7aa9cea372e4624bf7f64))

- **job**: Detached exec must record the exit code, not just the output
  ([`804776f`](https://codeberg.org/cyteena/inspire-cli/commit/804776fe04255315edd74358eedcc0567e136a87))

- **job**: Exec failures name their cause instead of one hint for three platform states
  ([`b3484af`](https://codeberg.org/cyteena/inspire-cli/commit/b3484afa22a4a31f055859e96a538cb4e1abb36b))

- **job**: Job logs --json answers the canonical items/total shape
  ([`12edc9d`](https://codeberg.org/cyteena/inspire-cli/commit/12edc9d5943aa4edba5d1e193b6cc076f24d1d6e))

- **job**: Name the metric types the platform accepts, and stop implying memory is fetched
  ([`b0e0723`](https://codeberg.org/cyteena/inspire-cli/commit/b0e07239805b1604e251298504e06a73be097e33))

- **job**: Quote the scheduler's reason when a job has not started
  ([`1585a7e`](https://codeberg.org/cyteena/inspire-cli/commit/1585a7e59924354f2bbfbdb07ad8b05503ad2546))

- **job**: Render fault tolerance as attempts, and show the phase timeline
  ([`e4ca94b`](https://codeberg.org/cyteena/inspire-cli/commit/e4ca94b405aa62f22cd14905cbe8f1bc2978b239))

- **job**: Say what an empty instance list actually means
  ([`51da1eb`](https://codeberg.org/cyteena/inspire-cli/commit/51da1ebefe8f443323a9335eac27a811f5ebedea))

- **job**: Say when an exec failure is safe to retry
  ([`48029a8`](https://codeberg.org/cyteena/inspire-cli/commit/48029a8fc03210adcaf99bf2a2040cbc24f6ddee))

- **job**: Separate "no such spec here" from "this spec is not for your priority"
  ([`b4745c6`](https://codeberg.org/cyteena/inspire-cli/commit/b4745c61c67b12c363a8a96fe9c799c5f6a5c21c))

- **job**: Show the wall-clock ceiling next to elapsed running time
  ([`3c59f09`](https://codeberg.org/cyteena/inspire-cli/commit/3c59f09f262296430ae0af63cae4c6e2156f0a5a))

- **job**: Stop labelling a capped priority as if the CLI value were used
  ([`1455027`](https://codeberg.org/cyteena/inspire-cli/commit/145502705fbc82ccca23c4030a9f5782c35d7c9a))

- **job**: The quota hint now pastes back into --quota
  ([#75](https://codeberg.org/cyteena/inspire-cli/pulls/75),
  [`133dfa1`](https://codeberg.org/cyteena/inspire-cli/commit/133dfa14cb3b51f00942010162e3a87a49405ee4))

- **job**: Upload failures stop claiming cleanup that was never observed
  ([`2d4eebd`](https://codeberg.org/cyteena/inspire-cli/commit/2d4eebda1a835b2b679f94ff0d5d1dfca307bcb5))

- **notebook**: A timeout must not echo the encoded remote environment
  ([`0aa7274`](https://codeberg.org/cyteena/inspire-cli/commit/0aa727419c0a3e5214cf9ae474b121202d6c6a79))

- **notebook**: Report an exec timeout as a timeout, and stop printing our own framing
  ([`c07d05e`](https://codeberg.org/cyteena/inspire-cli/commit/c07d05e695db2bebd12383c0b5b1ae0db59d2b8f))

- **platform**: Refresh the web session when the exec socket is rejected
  ([`afdba38`](https://codeberg.org/cyteena/inspire-cli/commit/afdba389029361bf643de9996fb331850c18fba1))

- **scripts**: Make the ablation matcher count option references, not character collisions
  ([`3804d9a`](https://codeberg.org/cyteena/inspire-cli/commit/3804d9a47c5f11fdf33f0ec2fa5d1d07ff11d6d3))

- **scripts**: Stop the ablation harness from being satisfied by its own unit test
  ([`383e715`](https://codeberg.org/cyteena/inspire-cli/commit/383e71589f5d2fb69cbbd2e8ec52e7faadb963d1))

- **scripts**: Stop the mutator passing a selector list where pytest expects a path
  ([`b62cb3c`](https://codeberg.org/cyteena/inspire-cli/commit/b62cb3cbb6756e8848c9b581338075560910bf1d))

- **security**: Quote remote-command interpolation and stop prefilling secrets (#62 #63 #64)
  ([`d67bc49`](https://codeberg.org/cyteena/inspire-cli/commit/d67bc492949269f45c85e74d1eb78c68fbe5858a))

- **security**: Verify TLS on every cookie-carrying channel, with one loud opt-out
  ([#65](https://codeberg.org/cyteena/inspire-cli/pulls/65),
  [`93e4b2a`](https://codeberg.org/cyteena/inspire-cli/commit/93e4b2a2d9284e5138b1b5f6cfffa915acae0fce))

- **seiso**: Match the report directory with the patterns seiso actually honours
  ([`05b4ec8`](https://codeberg.org/cyteena/inspire-cli/commit/05b4ec8cf7c2e18fcf4d2ac64a8021f5e186d533))

- **storage**: Say what a denied write means instead of emitting "403 Forbidden"
  ([`7c8db19`](https://codeberg.org/cyteena/inspire-cli/commit/7c8db193f22bfdde483a9d5a19223eb304a9e726))

- **storage**: Stop reporting a cleanup success when the probe created nothing
  ([`c92ddd3`](https://codeberg.org/cyteena/inspire-cli/commit/c92ddd35bb1f5e5b1f377de131e3915e72f3624b))

- **test**: Order the TERM-trap cancel test behind a payload readiness file
  ([`a7ae5da`](https://codeberg.org/cyteena/inspire-cli/commit/a7ae5da144535db06945c689d9c4e9bf05de4a1c))

- **test**: Poll for the payload's pid file in the group-cancel test
  ([`ba4a16e`](https://codeberg.org/cyteena/inspire-cli/commit/ba4a16e445c48a18db6481f5df53af27d786030c))

- **tunnel**: Make `ssh-config` output something ssh can read
  ([`f77adc5`](https://codeberg.org/cyteena/inspire-cli/commit/f77adc5edc8378c524f4c920ef8efdcc5422c6dc))

- **upload**: A failure must name the partial file it left behind
  ([`ca236a6`](https://codeberg.org/cyteena/inspire-cli/commit/ca236a66d10370473f33884bd862b6d712f477f0))

- **upload**: Derive the partial name, so retries reuse one instead of littering
  ([`74e1678`](https://codeberg.org/cyteena/inspire-cli/commit/74e167809337eecf49dc1945558fa7c141108f19))

- **upload**: Retry the read-only pre-flight, because that is where uploads were dying
  ([`6126378`](https://codeberg.org/cyteena/inspire-cli/commit/6126378974edadaeb3b97c7efe39e6f15235c734))

- **upload**: Stop the uploader from killing its own channel, and stop leaking bytes in errors
  ([`fedaddc`](https://codeberg.org/cyteena/inspire-cli/commit/fedaddcaeeaefae683034f13d35f7af13abeaaa6))

### Features

- **cli**: --dry-run previews on every create command (#76 S2b)
  ([`a90b2cf`](https://codeberg.org/cyteena/inspire-cli/commit/a90b2cf968f993d683d58cec2927941e1285e69a))

- **cli**: Inspire api — a raw platform escape hatch (#76 S4)
  ([`e332f39`](https://codeberg.org/cyteena/inspire-cli/commit/e332f394e548ab79910c2a7cb884b952c316b4e1))

- **cli**: Inspire schema, risk labels, and an agent quickstart (#76 S1)
  ([`d5f717b`](https://codeberg.org/cyteena/inspire-cli/commit/d5f717bdc1413507f27cc515bb7336cdb97decd6))

- **cli**: Job create and run refuse to duplicate an active name (#76 S2b)
  ([`721aa8d`](https://codeberg.org/cyteena/inspire-cli/commit/721aa8d866ce73887043a75e0f22e1a3b41b4835))

- **cli**: One canonical {items, total} list payload shape (#46 #76 S5-2)
  ([`81e9b19`](https://codeberg.org/cyteena/inspire-cli/commit/81e9b19f6e11a288ee81375220947162d7f0cee2))

- **cli**: One confirmation spelling, --yes everywhere (#76 S2a)
  ([`0d7ad4f`](https://codeberg.org/cyteena/inspire-cli/commit/0d7ad4fe8da49a37fca79a626bf42fea7ab58fb8))

- **cli**: Ship agent skill manuals with platform semantics
  ([`fa2aeb2`](https://codeberg.org/cyteena/inspire-cli/commit/fa2aeb27383b637e2b4bf442a1228ab6b9bffa44))

- **cli**: The agent contract, written down and pinned by tests (#76 S0)
  ([`e0308cb`](https://codeberg.org/cyteena/inspire-cli/commit/e0308cb1c46300b9bb221289aa5f5470909f5037))

- **config**: Report keys the loader silently ignores
  ([`5d90f13`](https://codeberg.org/cyteena/inspire-cli/commit/5d90f133f3cdbb70b53ae236fe2f456aee6fcef7))

- **exec**: One PTY session can carry many commands, which is the fix for today's drops
  ([`5aa6ad8`](https://codeberg.org/cyteena/inspire-cli/commit/5aa6ad809854879371d5a752b08bee19fa979607))

- **exec**: Separate readiness wait from command timeout, and make detached runs cancellable (#50
  #54)
  ([`d448ef6`](https://codeberg.org/cyteena/inspire-cli/commit/d448ef64a6f6a65d7efb172c81a789482a990453))

- **image**: Filter images by visibility, the axis --source never described
  ([`3595ff6`](https://codeberg.org/cyteena/inspire-cli/commit/3595ff61f3c66cfd6c2bf51f60832c433c29bc96))

- **job**: `job exec --stdin`
  ([`9df2732`](https://codeberg.org/cyteena/inspire-cli/commit/9df2732d2027f13c34b61a8714ddde023067bac3))

- **job**: `job wait --until running|failed|unschedulable`, with the evidence attached
  ([`f0a38a1`](https://codeberg.org/cyteena/inspire-cli/commit/f0a38a1e304f7839947ded16fa78123238f7068f))

- **job**: Detached exec records, so a dropped channel stops losing answers
  ([`1ea5e93`](https://codeberg.org/cyteena/inspire-cli/commit/1ea5e932e557c1d85c316792d25ddb98d7801e2d))

- **job**: Quota-usage surfaces live quota consumption (#76 S3)
  ([`8b49246`](https://codeberg.org/cyteena/inspire-cli/commit/8b49246200f8f134483788e766d44399b9aaf062))

- **job**: Upload over ONE PTY session, which is what makes it work at all
  ([`15120de`](https://codeberg.org/cyteena/inspire-cli/commit/15120de5879a4ac50748f28a62bf6227d8a0c92b))

- **notebook**: `--dry-run` for uploads, and withdraw a metric claim I got wrong
  ([`774a5b1`](https://codeberg.org/cyteena/inspire-cli/commit/774a5b1d02bf0f7d2d76a9837ee60ae19054fd9d))

- **notebook**: Expose `inspire notebook upload`
  ([`96378e4`](https://codeberg.org/cyteena/inspire-cli/commit/96378e4ed08021ce72a57e3b5ee08b0249fd87af))

### Refactoring

- **docs**: Give the live-suite environment one owner and move plans into issues
  ([`2dd1c05`](https://codeberg.org/cyteena/inspire-cli/commit/2dd1c05b6feea7374eda78989df5e85368f0b9d3))

- **formatters**: Drop a dead ternary I committed by accident
  ([`b8cb49e`](https://codeberg.org/cyteena/inspire-cli/commit/b8cb49e6cfeee576b672be5556db1f6f8d6db25b))

- **image**: Give the fallback push target one owner, then pin it
  ([`5240be0`](https://codeberg.org/cyteena/inspire-cli/commit/5240be0d41a1caba981303875f3e693335479408))

- **job**: Remove the legacy SSH/local-cache log path from job logs
  ([`5225b4c`](https://codeberg.org/cyteena/inspire-cli/commit/5225b4c63d40d3ad1c4173ec31ea6377a78fadb3))

- **job**: Remove the local job cache; push status filtering to the platform
  ([#70](https://codeberg.org/cyteena/inspire-cli/pulls/70),
  [`d434dfd`](https://codeberg.org/cyteena/inspire-cli/commit/d434dfde21d70d4425340660806cc48c2d280178))

### Breaking Changes

- **job**: Job logs now reads only the platform /logs/train API. The legacy path - log files written
  into INSPIRE_TARGET_DIR, synced through the Gitea bridge workflow, fetched over SSH, cached
  locally with per-job read offsets - is deleted, along with its flags:


## v0.4.0 (2026-09-28)

### Bug Fixes

- **browser**: Report a missing Chromium binary as the install step it is
  ([`d7d5af0`](https://codeberg.org/cyteena/inspire-cli/commit/d7d5af0a590067040ea9657789b31f4ebf062c7c))

- **ci**: Gate the release on the built wheel before pushing it
  ([`2fe7839`](https://codeberg.org/cyteena/inspire-cli/commit/2fe78398a921c2082a7943141141deb928fdfd2a))

- **ci**: Redo the release from the new tip when the push loses a race
  ([`8fedb39`](https://codeberg.org/cyteena/inspire-cli/commit/8fedb396d5226a91d63007b41061bd68e657c034))

- **ci**: Run one pinned toolchain everywhere so local and CI cannot disagree
  ([`7341e48`](https://codeberg.org/cyteena/inspire-cli/commit/7341e485010960acc60c377e4c6a7105a896eeab))

- **ci**: Stop skip-ci markers in commit bodies from swallowing the push
  ([`241f4f5`](https://codeberg.org/cyteena/inspire-cli/commit/241f4f516c79e44e3e376f1fe0143f85e75cad80))

- **cli**: Keep machine-mode output on stdout and nothing else
  ([`7289056`](https://codeberg.org/cyteena/inspire-cli/commit/7289056be060e96ce0a29f8523b16f81de113148))

- **config**: Report env overrides of what we download, execute and trust
  ([`8a9e57f`](https://codeberg.org/cyteena/inspire-cli/commit/8a9e57f8a71b4137cfe2b87288bf3d86e630c65d))

- **image**: Treat SUCCESS as a ready image when waiting for one
  ([`aca930f`](https://codeberg.org/cyteena/inspire-cli/commit/aca930fb5db661fae0a765c96d73e6684e24c213))

- **job**: Drop the upload chunk size to something a real PTY accepts
  ([`9eb2977`](https://codeberg.org/cyteena/inspire-cli/commit/9eb29777dbd56bbbbe94088a9c05ea4ce363f054))

- **job**: Give `job update` a help text that says what it actually does
  ([`2312fc6`](https://codeberg.org/cyteena/inspire-cli/commit/2312fc6695c252e5b31d85b24615f63ddc37e0f1))

- **job**: Give job exec the same remote runtime as every other exec path
  ([`2f1082d`](https://codeberg.org/cyteena/inspire-cli/commit/2f1082d7dea29df3321e535420c9974a1b24537e))

- **job**: Select fault-tolerance attempts by number and announce the choice
  ([`28816fc`](https://codeberg.org/cyteena/inspire-cli/commit/28816fc63996c495cd35c2181f5e81aecd0ae78f))

- **job**: Treat a reserving round as its own attempt class
  ([`67b4aff`](https://codeberg.org/cyteena/inspire-cli/commit/67b4aff8cfec0c7e3c635b560199b94779bfb11b))

- **lint**: Enforce bugbear and close the silent-failure sites it flagged
  ([`0ecb7fa`](https://codeberg.org/cyteena/inspire-cli/commit/0ecb7fa0067db21e4a02b8f9f1360dc5e82bf400))

- **mount**: Make mount list work in both output modes
  ([`3c8a3c9`](https://codeberg.org/cyteena/inspire-cli/commit/3c8a3c9ec1062e8deef4a7e356ce23728d792f3d))

- **mount**: Report mount status as output, not as hidden diagnostics
  ([`d68a80c`](https://codeberg.org/cyteena/inspire-cli/commit/d68a80c1b967392f841c805d88d379bb25e985a5))

- **notebook**: Apply remote_env to notebook exec as well
  ([`6ab27d0`](https://codeberg.org/cyteena/inspire-cli/commit/6ab27d06bd6df9632eb92d87b1b82757d97ffcef))

- **notebook**: Diagnose by name, and degrade when Chromium is absent
  ([`17d1ead`](https://codeberg.org/cyteena/inspire-cli/commit/17d1ead88b29b5a8d14fd8d3cf503938cf06d07e))

- **notebook**: Retry terminal creation while JupyterLab finishes booting
  ([`80acab3`](https://codeberg.org/cyteena/inspire-cli/commit/80acab343fac5e6cf48760986f074366df87a840))

- **security**: Stop credential files from re-issuing their own loose mode
  ([`1791f94`](https://codeberg.org/cyteena/inspire-cli/commit/1791f94fda6d312ebde4fc1b2a8022eb401f5024))

- **security**: Stop leaving notebook tokens readable by other local users
  ([`efae69a`](https://codeberg.org/cyteena/inspire-cli/commit/efae69a024560566302b6d417a47b5bd5ba2e772))

- **tunnel**: Verify the rtunnel binary we exec, and pin an immutable release
  ([`2b69e83`](https://codeberg.org/cyteena/inspire-cli/commit/2b69e83eeac1887d3fa383873771aa1f4263a086))

### Features

- **cli**: Remove the experimental mount command
  ([`c6953e4`](https://codeberg.org/cyteena/inspire-cli/commit/c6953e47f106720fa82491ab16e2f4772239550d))

- **config**: Warn when account-scoped values come from the environment
  ([`a800f32`](https://codeberg.org/cyteena/inspire-cli/commit/a800f320967e21b10a4044b9e18ede0559644cfc))

- **job**: Upload files through a running job's exec channel
  ([`cacae41`](https://codeberg.org/cyteena/inspire-cli/commit/cacae41026331e474d2d6a06e3c7d107e2f43603))

- **notebook**: Report exposed listeners and key drift over the Jupyter transport
  ([`83e3f55`](https://codeberg.org/cyteena/inspire-cli/commit/83e3f5521a96d10013f8e17ca8e0af276e1a79ba))

- **transport**: Upload through the exec channel, verified before it becomes visible
  ([`694b656`](https://codeberg.org/cyteena/inspire-cli/commit/694b656dce28f46d5d2523a7f1d3ff8906fff4fb))


## v0.3.0 (2026-09-26)

### Feat

- **job**: surface per-offer price and raw passthrough in quota
- make GPU notebook bridges agent-friendly
- add account-scoped discovery state
- add account-scoped workload operations
- add platform feature parity commands and live e2e coverage
- notebook exec returns stdout output
- make connection pool the default for bridge exec
- add Paramiko connection pool for bridge exec
- add notebook doctor/exec suggestions to SSH error messages
- **notebook**: add deep SSH diagnostics to doctor in verbose mode
- **ssh**: configurable port, debug logging, and comprehensive tests
- **tunnel**: improve tunnel list command with column control, limit, and notebook names
- **cli**: add resources specs command and workspace-aware auth
- **openapi**: make ResourceManager workspace-scoped
- **openapi**: add workspace_specs module for per-workspace GPU specs
- **config**: add workspace_specs and workspace_names fields
- **notebook**: add --tunneled flag to show only notebooks with SSH tunnels
- **notebook**: add column control and tunnel priority sorting to list command
- **cli**: add --json flag to all commands for consistency
- detect sshd installation failure and surface actionable error
- add `rtunnel_upload_policy` setting (auto/never/always)
- skip redundant rtunnel upload via SHA-256 hash sidecar
- add --verbose flag to `tunnel list`, default to compact one-line output
- add auto_fault_tolerance, fix priority=0 bug, add compute diagnostics
- support `--` command passthrough for `notebook ssh`
- add --source personal-visible and platform-aware rtunnel URLs
- add debug logging for rtunnel download, upload, and setup
- store project name in local job cache
- detect congested projects with Unschedulable jobs
- add --force flag to inspire sync for diverged branches
- upload rtunnel binary to notebooks via Jupyter Contents API
- apt mirror dropbear install, project_order priority fix, setup skill overhaul
- prefer gpu_limit=false projects in auto-selection to avoid indefinite queuing
- resolve partial UUID prefixes for job, notebook, and image commands
- add project_order config for user-defined project selection priority
- show richer detail in notebook status output

### Fix

- **ci**: give the release job a commit identity
- **cli**: align auth checks, cached specs, and scripted discovery
- **rtunnel**: default dropbear host key dir to user shared root
- **rtunnel**: persist dropbear host key and detach ssh daemons
- **job**: stream job stdout via tee so platform /logs/train captures it
- **job**: resolve selector workspaces and project names in job list
- **job**: submit via account-scoped session and train_job/create API
- honor training offer priority constraints
- use aggregate capacity for single-GPU auto-select
- **job**: route and parse RTX 4090 workloads
- **job**: select schedulable request-fit nodes
- **job**: report fair priority downgrades
- **job**: reject unsupported fair priorities
- show platform job priority semantics
- restore low-priority training recovery
- redact job create JSON output
- separate low priority from fault tolerance
- cool down repeated CAS failures
- make queued job submission resilient
- retry transient JupyterLab resolution
- make notebook auto-selection capacity-safe
- harden notebook SSH proxy verification
- redact secrets in job inspection output
- harden account discovery and notebook workflows
- avoid auth during cached resource resolution
- isolate bridge tests from INSPIRE_TARGET_DIR
- keep rtunnel tests independent of playwright
- pin CI lint tool versions
- use platform APIs for job list and logs
- **bridge**: make BridgeProfile.has_internet computed from gpu_type
- **sync**: remove _git_rev_count guard in bundle fallback path
- notebook exec WebSocket stdout capture now works reliably
- add file redirect fallback for notebook exec stdout capture
- fall back to keyboard automation when JupyterLab frame is inaccessible
- use jupyter_url from API for notebook exec navigation
- handle admin-only access for cluster_nodes/list endpoint
- remove immediate pool close to allow connection reuse
- remove unused use_pool parameter from try_exec_via_ssh_tunnel
- remove alice-specific hints from error messages
- **CI**: add hypothesis to dependency-groups
- **cli**: remove OK footer from bridge exec success output
- **cli**: make --quiet default for bridge exec; remove duplicate deps
- **cli**: make --quiet default for bridge exec; remove duplicate deps
- use git diff for faster content comparison
- **cli**: stabilize --json error routing and logger cleanup
- **sync-script**: avoid unbound cleanup vars under set -u
- **notebook**: scope personal image lookup to selected workspace
- **ssh**: align get_ssh_command_args with stdio:// ProxyCommand
- **rtunnel**: add rm -f before cp to prevent ETXTBSY
- strip whitespace from environment variable reads
- **tests**: use relative path for config set command tests
- notebook image search fallback behavior
- **package**: resolve module naming conflicts for wheel builds
- **notebook**: normalize notebook ID format for tunnel matching
- restore shared sync ssh execution
- resolve rtunnel binary path mismatch between Contents API and $HOME
- update codex-chat skill with two-step tmux send pattern
- skip permission-error test when running as root
- update tunnel list test for compact output format
- keep rtunnel defaults lazy in config imports
- add CLAUDE.md and docs/ to github-public exclusion list
- rename Contents API rtunnel filename to avoid Jupyter hidden-file restriction
- skip curl fallback for offline notebooks and add rtunnel failure warnings
- chunk WebSocket stdin and wait for shell prompt before sending
- auto-delete browser terminals after notebook SSH setup
- terminal focus failure on notebook tunnel setup
- guard Playwright sync API from asyncio event loops
- resource-aware CPU compute group selection and docker_registry auto-detection
- multi-workspace compute group discovery and CPU group selection
- unify tunnel reconnect retries and JSON CLI behavior
- skip gpu_limit preference for CPU workloads in project selection
- run command sends jobs to wrong compute group, causing perpetual queuing
- force_refresh bypasses stale session cache, project list uses config workspaces
- project list now queries all workspaces
- match compute groups by name when location is empty
- let project_order take precedence over job_project_id env var
- wait for setup script completion via WS marker instead of fixed 3s delay
- support raw .deb dropbear packages and wait for shell prompt before WS send
- stop treating plain-text 404 as rtunnel-ready and auto-restart dropbear
- detect notebook failure immediately and fix resources nodes
- prevent pkill -f self-matching through bridge exec
- select project by priority, not unreliable GPU-hours signal

### Refactor

- **notebook**: simplify GPU quota matching with gpu_pattern substring match
- **cli**: make --json global-only across commands
- **logging**: clean JSON output and prevent info leaks in --json mode
- standardize CLI output for consistent error handling and messaging
- **notebook**: omit ID column from default list view
- **tunnel**: unify error handling with exit_with_error helper
- split rtunnel.py into rtunnel/ package (8 submodules)
- use constant for rtunnel URL default, remove redundant SOURCE_PRIVATE branch
- extract shared fault-tolerance and diagnostics helpers
- decompose high-complexity modules and standardize output/exception handling
- standardize error emission with central emit_error/exit_with_error
- remove unused mirror config options

## v0.2.4 (2025-01-01)

### Features

- Job management commands (create, status, logs, list, stop, wait)
- Notebook management commands (list, create, start, stop, ssh)
- Resource availability listing (GPUs, nodes)
- Quick job submission with auto-resource selection (`run`)
- Code sync to Bridge runner (`sync`)
- Bridge remote execution (`bridge exec`, `bridge ssh`)
- SSH tunnel management (add, remove, status, list, ssh-config)
- Configuration management (show, check, env) with TOML + env var loading
- Project initialization with environment detection
- Dual execution paths: SSH tunnel (fast) and Gitea/GitHub Actions (fallback)
- Human-readable and JSON output formatting
- Remote environment variable injection via `[remote_env]` config
