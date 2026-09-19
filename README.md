# Mac 清理助手（mac-cleaner）

> 一个本地运行的 macOS 磁盘清理工具：**先预览、再确认、可恢复**地清理系统缓存、日志、临时文件与废纸篓残留。
> 后端纯 Python 3 标准库（零第三方依赖），前端 Vite + React，一条命令启动，只监听 `127.0.0.1`。

## 功能特性

- **扫描并预览**：13 个可清理类目逐项列出，显示每条路径的体积、最后访问时间与风险等级
- **选择性清理**：按类目 / 单项勾选，支持筛选、排序、搜索
- **一键全清**：默认只勾选安全类目，个人文件类（下载目录大文件、iOS 备份）**永不默认勾选**
- **安全保护机制**：五道关卡串行校验 + 硬编码黑名单 + SIP 保护，任何一条路径都逃不过
- **清理前确认**：弹窗展示「N 项 / X GB / Top5 路径 / 可恢复性」；主目录外项目需额外勾选，高危类目需手动输入「确认删除」
- **可恢复**：默认「移到废纸篓」模式（`~/.Trash`），不是 `rm -rf`
- **历史与统计**：记录每次清理的成功/跳过/失败与释放量，累计释放空间总量，明细保留最近 5 次
- **中断保护**：扫描与清理均可随时中止，已完成部分照常保留并入库
- **权限降级**：无权限目录自动跳过并给出授权引导，绝不中断整体流程

## 支持的清理类目

| 类目 | 分组 | 风险 | 默认扫描 | 说明 |
| --- | --- | --- | --- | --- |
| 用户缓存 | 缓存 | 低 | ✅ | `~/Library/Caches` |
| 应用缓存 | 缓存 | 低 | ✅ | 沙盒容器 / Group Containers / Application Support 下的缓存 |
| 系统级缓存 | 缓存 | 中 | ✅ | `/Library/Caches`（位于主目录外，需额外确认） |
| 用户日志 | 日志 | 低 | ✅ | `~/Library/Logs`，只清内容保留目录 |
| 系统日志 | 日志 | 中 | ❌ | `/Library/Logs`、`/private/var/log`，通常需完全磁盘访问权限 |
| 临时文件 | 临时 | 低 | ✅ | `/private/var/folders/*/{T,C}`、`/tmp` |
| 回收站残留 | 回收站 | 低 | ✅ | `~/.Trash` |
| 下载目录大文件 | 个人 | 高 | ✅ | ≥100 MB 且 ≥30 天未访问，**默认不勾选** |
| Xcode 开发垃圾 | 开发者 | 中 | ❌ | DerivedData / Archives / DeviceSupport / CoreSimulator |
| 包管理器缓存 | 开发者 | 低 | ❌ | npm / Yarn / pnpm / Homebrew / Cargo / Gradle / Maven |
| 浏览器缓存 | 隐私 | 中 | ❌ | Safari / Chrome / Firefox / Edge / WebKit |
| iOS 设备备份 | 个人 | 高 | ❌ | 需输入确认文本；删除后不可恢复 |
| 外置卷残留 | 外置卷 | 中 | ❌ | `/Volumes/*/.Trashes`、`.Spotlight-V100` |

类目根路径不存在时呈「未检测到 / 未安装」，**不会报错**。

## 安全设计

每条待清理路径在执行前必须串行通过五道关卡，任一道不通过立即短路拒绝并写入审计日志：

| 关卡 | 作用 | 拒绝码 |
| --- | --- | --- |
| ① 路径规范化 | 空串 / 根目录 / `~` / `$HOME` 熔断，`realpath` 解析后校验沙箱逃逸 | `empty_path` `root_path` `path_traversal` |
| ② 白名单 | 必须位于某个**启用类目**的展开根路径之下 | `not_in_whitelist` |
| ③ 黑名单 | 26 条受保护路径（`/System`、`/usr`、`~/.ssh` 式隐私目录、`~/Documents` 等）一律拒绝 | `blacklist` |
| ④ SIP 保护 | 命中系统完整性保护前缀只读跳过 | `sip` |
| ⑤ 占用检测 | 文件独占打开 / 目录可写探测，被占用绝不重试强删 | `in_use` `permission_denied` |

其他防护：

- **不接受客户端路径**：清理请求只传条目 `id`，服务端自行回查真实路径，杜绝路径伪造
- **防 TOCTOU**：预览生成 10 分钟有效期的服务端快照令牌，执行时必须携带
- **dry-run**：预览阶段零写操作
- **单次上限**：默认 20 GB / 50000 条，超限提示分批
- **审计留痕**：所有被拦截的路径写入 `data/audit.log`（按 5 MB 滚动保留 3 份）

## 快速开始

环境要求：**macOS** + **Python 3.8+**（运行）；**Node.js 18+**（仅首次构建前端，可选）

```bash
# 1. 进入项目目录
cd mac-cleaner

# 2. 一条命令启动（缺失前端产物时会自动 npm install && npm run build）
python3 start.py
```

浏览器会自动打开 `http://127.0.0.1:8765`。常用参数：

```bash
python3 start.py --no-browser      # 不自动打开浏览器
python3 start.py --port 9000       # 指定起始端口（占用时自动 +1）
python3 start.py --build           # 强制重新构建前端
python3 start.py --sandbox /tmp/x  # 沙箱模式：所有扫描与清理仅在指定目录内进行（调试用）
```

## 项目结构

```
mac-cleaner/
├── start.py                  # 一条命令启动入口
├── server/                   # 后端（零第三方依赖）
│   ├── config.py             # 路径常量 / 错误码 / 原因码 / 默认设置
│   ├── categories.py         # 13 个类目定义（白名单唯一来源）
│   ├── safety.py             # 五道安全关卡
│   ├── scanner.py            # 并发扫描引擎（可沙箱注入）
│   ├── cleaner.py            # 清理引擎（预览快照 / 废纸篓 / 历史）
│   ├── store.py              # SQLite 历史、设置、日志
│   ├── api.py                # 18 个 REST 路由 + 静态资源托管
│   └── server.py             # ThreadingHTTPServer（仅绑 127.0.0.1）
├── web/                      # 前端（Vite + React 18 + MUI 5 + Tailwind 3）
│   └── src/pages/            # 首页 / 扫描清理 / 历史记录
├── tests/                    # 46 项自动化测试
└── docs/                     # PRD、架构设计、时序图、类图
```

## 测试

```bash
python3 -m unittest discover -s tests -p "test_*.py" -v
```

| 测试文件 | 覆盖内容 |
| --- | --- |
| `tests/test_safety.py` | 五道关卡：26 条黑名单逐条验证、`..` 穿越、软链逃逸、空/根熔断、上限校验 |
| `tests/test_scanner_sandbox.py` | 沙箱扫描：体积与 `du` 偏差 ≤5%、软链不递归、权限降级、取消 |
| `tests/test_clean_e2e_sandbox.py` | 清理全链路：扫描 → 预览 → 确认 → 执行 → 移入废纸篓 → 写历史；含四类拒绝路径 |

全部测试都在临时沙箱内运行，**可在真机上安全执行，不会触碰你的真实文件**。

## 技术栈

- **后端**：Python 3 标准库（`http.server` / `sqlite3` / `concurrent.futures` / `os.scandir`），无 `requirements.txt`
- **前端**：Vite 5 + React 18 + MUI 5 + Tailwind CSS 3
- **存储**：SQLite（`data/mac_cleaner.db`）
- **网络**：仅监听 `127.0.0.1`，不对外暴露，不联网

## 注意事项

- 默认「移到废纸篓」模式下，**必须清空废纸篓才会真正释放磁盘空间**；本工具的废纸篓模式使用文件移动实现，Finder 的「放回原处」不可用，但可手动拖回
- 切换到「直接删除」模式后文件不可恢复，请谨慎使用
- 访问 `/Library`、`/private/var/log` 等系统目录通常需要在「系统设置 → 隐私与安全性 → 完全磁盘访问权限」中授权
- 清理 Xcode 相关类目后，下次编译会变慢、设备支持可能需要重新下载
- 项目数据全部位于 `data/` 目录，卸载即删除项目目录，无系统残留
