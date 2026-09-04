# Receipt Sync Agent Handoff

本文档供接手本项目的编码 agent 使用，尤其是将在 Mac 上用 Xcode 继续开发和测试 iPhone 客户端的 agent。它记录了截至 2026-09-03 的需求对话、技术选择、完成状态、运行方法、安全边界和下一步。面向普通用户的简明说明仍以 `README.md` 为准。

## 1. 工作目录和事实来源

- 实际源码根目录是本文件所在目录：`work/receipt-sync-app/`。
- `outputs/receipt-sync-app/` 是较早的导出副本，不是继续开发的事实来源，不要在其中修改代码。
- 当前上层目录不是 Git 仓库；不要假定存在提交历史、分支或可用的 `git diff`。
- Windows 上已有用户测试数据位于 `work/pc-local-test/`。未经用户明确同意，不要删除、清空、迁移或覆盖这些记录。
- 正式运行数据默认位于 `%LOCALAPPDATA%\ReceiptSync`，也不得在测试时随意改动。
- 测试用的数据库、证书、图片和模型目录属于运行产物，不应复制到 Mac 工程、提交或分发。
- 配对令牌、TLS 私钥和当前证书指纹属于敏感信息，不要写入源码、文档、日志样例或聊天回复。

## 2. 用户目标和完整需求脉络

以下是本项目对话中形成的需求和修正，按发生顺序整理。这里记录的是需求含义和决策，不是逐字聊天转录。

1. 最初目标是制作一份用于日常开支统计的会计财务表格，全部使用港币 HKD。已生成过独立工作簿 `outputs/daily-expense-tracker-hkd/日常开支统计表_HKD.xlsx`；当前工作的重点已经转为自动小票录入与手机同步应用。
2. 用户电脑已安装 PaddleOCR，希望用它识别小票并自动录入。曾用网上的小票示例以及用户提供的多张香港小票图片测试。
3. 报表既要直观，也要体现每个商品的花费。曾考虑用 DeepSeek 做商品分类，但当前版本没有接入 DeepSeek，也不能把敏感小票内容默认发送给第三方。
4. 总账必须同时覆盖购物、通勤、吃饭及收入，电脑负责把总收入、总支出和大类汇总返回手机。
5. 独立商品明细被确认可行：总收支流水和商品明细分层保存，避免主流水过度膨胀。
6. 餐饮小票必须列出每道具体菜品和有价饮品。只有真正的非菜品费用才合并，不是把饮料等消费合并。
7. “非菜品”特指服务费、茶芥、附加费和收款舍入等费用。这些费用统一成一条 `餐饮非菜品（合并）`，无需再分项计算。
8. HK$0 的 `Welcome`、`不用加配`、免费项目等不应录入明细。
9. 商店小票可能先印价格、下一行才印商品名；解析器必须支持这种版式。例如用户的小票中纸袋价格是 HK$2，而不是旁边或下一行的其他数字。
10. 餐饮小票的价格可能位于商品同一视觉行的右侧。曾发生拉面被误识别为 HK$1，原因是按 OCR 文本顺序抓到了数量/序号，而没有利用坐标选择同行最右侧价格。当前解析器用 OCR 坐标聚合视觉行并取最右金额，同时保留具体菜名。
11. 审核界面中不恰当的“（待确认）”展示已调整；确认小票后，大类、流水和汇总必须立即读取已确认数据并更新。
12. 单笔收入/支出录入曾出现不显示、未计入总收入的问题。当前已恢复，并统一按香港时区月份统计；保存后界面切换到该记录所在月份。用户确认相关记录确实在九月，因此月度切换和流水列表很重要。
13. 电脑审核页需要完整流水列表，展示日期、类型、大类、内容、金额和来源。
14. 支持删除：可删除整张小票及其关联流水、商品明细和原图，也可删除手工单笔录入；删除前确认，删除后重算汇总并原子重导出 CSV。
15. 手机负责拍照、离线排队、同步和看汇总；Windows 电脑负责 PaddleOCR、人工审核、SQLite 存储和大类汇总。电脑删除一张小票后，手机下次查询该已知 UUID 收到 404，应清除对应本地队列项。
16. 暂不打包，不需要 `.ipa` 或安装包；先在本机与真机开发环境测试。
17. 暂不设计外出访问、Tailscale 或 VPN。手机与电脑只考虑同一局域网；大学公共 Wi-Fi 可能有客户端隔离，必要时使用 iPhone 个人热点做直连测试。
18. 公共 Wi-Fi 上必须有安全措施。当前使用 HTTPS、自签名证书指纹固定和受限随机设备令牌；电脑审核管理面仅监听回环地址。
19. 用户提出可以尝试 Passkey。结论是当前不实现：变化的局域网 IP 缺少稳定的 WebAuthn relying-party 域名，且后台同步不应每次要求 Face ID。若以后有稳定 HTTPS 域名或中转服务，可用 Passkey 做首次配对或管理授权，再签发受限设备凭证。
20. Paddle 模型不能每次测试都下载到新缓存。正式服务和临时测试必须复用 `%LOCALAPPDATA%\ReceiptSync\paddle_models`；只有显式升级模型或手工删除缓存时才重新下载。

## 3. 当前产品行为

### Windows 电脑端

- 一个 Python 进程同时提供两个监听器和一个共享 OCR worker。
- `http://127.0.0.1:8764` 是仅限本机的审核管理页。
- `https://0.0.0.0:8765` 是 iPhone 加密同步接口；实际配对地址使用审核页检测到的局域网 IP。
- PaddleOCR 异步识别，结果在电脑端人工确认后才进入总收支。
- SQLite 保存小票、总流水和商品明细；原图保存在数据目录。
- 已确认数据导出为 UTF-8 CSV：`exports/transactions.csv` 和 `exports/line_items.csv`。
- 月度边界按 `Asia/Hong_Kong`/UTC+8 计算，而不是按服务器 UTC 日期直接截字符串。
- 金额以整数分存储，币种固定为 HKD，避免浮点误差。

### iPhone 端

- SwiftUI 应用，最低 iOS 17，项目为 `ios/ReceiptSync.xcodeproj`。
- 拍照后 JPEG 压缩并写入 app 本地目录，立即尝试上传；电脑不可达时保留队列并重试。
- 后台刷新只是 iOS 提供的机会性重试，不能承诺固定同步周期。
- 手机显示所选月份的总收入、总支出、结余、收入大类、支出大类和待处理小票数。
- 汇总由电脑数据库计算，手机只展示，不自行重算总账。
- 手机可以新增手工收入/支出；金额必须大于零并按 HKD 分四舍五入。
- 配对令牌保存在 Keychain，使用 `kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly`。
- TLS 客户端以 SHA-256 固定服务器叶证书。证书不匹配时必须拒绝连接。

## 4. 技术栈和关键文件

### 电脑端

- Python 标准库 HTTP 服务：`http.server.ThreadingHTTPServer`。
- TLS：Python `ssl`，最低 TLS 1.2；PowerShell 生成 3072 位 RSA 自签名证书。
- 数据库：SQLite 3，表为 `receipts`、`transactions`、`line_items`。
- OCR：PaddleOCR 3.7.0，当前固定 `PP-OCRv6_medium_det` 与 `PP-OCRv6_medium_rec`。
- 前端：单文件原生 HTML/CSS/JavaScript，无 Node 构建步骤。
- 测试：Python `unittest`，不依赖 pytest。

关键文件：

- `pc/receipt_sync_server.py`：HTTP/HTTPS API、鉴权、SQLite、OCR worker、汇总、删除和 CSV 导出。
- `pc/receipt_parser.py`：OCR 行、视觉坐标、总计/小计、商品和餐饮分项解析规则。
- `pc/web/index.html`：电脑审核、配对、流水、手工录入和删除界面。
- `pc/test_receipt_sync.py`：22 个回归测试。
- `pc/start.ps1`：正式双端口单进程启动。
- `pc/generate_certificate.ps1`：兼容 Windows PowerShell 5.1 的证书和 PKCS#1 私钥生成。
- `start-service.cmd`：供用户双击的正式启动入口。
- `test-local.cmd`：只启动回环 HTTP 的本地 PC 测试入口，不能用于 iPhone。

### 手机端

- Swift 5、SwiftUI、UIKit 相机桥接。
- `URLSession` + `CryptoKit` 做 HTTPS 请求和证书 SHA-256 固定。
- Security.framework Keychain 保存同步令牌。
- BackgroundTasks/后台 fetch 做机会性队列重试。

关键文件：

- `ios/ReceiptSync/ContentView.swift`：汇总、月份切换、相机、设置和手工收支界面。
- `ios/ReceiptSync/ReceiptStore.swift`：离线队列、持久化、同步状态和月份刷新。
- `ios/ReceiptSync/SyncClient.swift`：受限 API 客户端和证书固定。
- `ios/ReceiptSync/Models.swift`：网络及本地数据模型。
- `ios/ReceiptSync/KeychainStore.swift`：令牌安全存储。
- `ios/ReceiptSync/AppDelegate.swift`：后台刷新注册与调度。
- `ios/ReceiptSync/CameraPicker.swift`：系统相机封装。
- `ios/ReceiptSync/Info.plist`：相机、本地网络和后台模式声明。

## 5. 解析和账务规则，不得回退

- 小票在 `confirmed` 之前绝不计入月度汇总或 CSV 正式流水。
- 餐饮：每个菜品/有价饮品一条 `餐饮菜品`；非菜品差额最多一条 `餐饮非菜品（合并）`。
- 餐饮非菜品金额优先按 `实付总额 - 小计` 得出，因此自然包含服务费和最终收款舍入；不要另建“舍入”条目。
- HK$0 欢迎项、免费项或无需加配项不进入明细。
- 有坐标时，同行金额取视觉上最右侧金额，避免把数量 `1` 当作菜价。
- 对价格先于商品名的超市版式，缓存价格行并绑定下一条有效商品名；折扣作用于前一个商品。
- 分项不可靠时保留警告供人工审核，不要为了让合计相等而虚构商品名或静默丢弃具体菜名。
- 手工流水 ID 使用 `manual-<UUID>`，不得覆盖小票流水 ID；小票流水不能通过“删除手工流水”接口删除。
- 删除小票必须一并删除其流水、明细和受管图片，但只能删除数据目录 `images` 内的文件。
- CSV 写入必须保持原子替换，避免同步或异常退出留下半份文件。

## 6. API 与安全边界

手机请求使用 `X-Sync-Token`。远程设备令牌只允许：

- `POST /api/v1/receipts`：上传 JPEG/PNG 小票，单张 32 字节至 12 MB。
- `GET /api/v1/receipts/<已知 UUID>`：查看这张手机已知小票的状态；不得带 `raw=1`。
- `GET /api/v1/summary?month=YYYY-MM`：读取大类汇总。
- `POST /api/v1/transactions`：新增手工收入或支出。

远程令牌不得访问：

- 全部小票列表、全部流水列表、配对信息或 OCR 原文。
- 确认、重新识别、删除小票或删除流水。
- 电脑审核页面。

只有本机回环连接且 Host 为 `localhost` 或回环 IP 时才能使用管理能力。不要为了方便 iPhone 调试而放宽这些边界。鉴权连续失败有速率限制；输入文本、金额、条目数和请求体大小均有限制。小票 ID 必须是规范 UUID，路径必须经过受管目录校验。

注意：`/api/v1/health` 当前可匿名访问，用于连通性判断，但不得返回配置、版本细节、令牌、账目或个人数据。

## 7. 如何在 Windows 运行

当前默认 PaddleOCR Python 路径为：

```text
C:\Users\15610\Documents\ChatGPT\DesktopAssistant\.venv-paddle\Scripts\python.exe
```

正式服务最简单的启动方法：

1. 在 Windows 打开 `work/receipt-sync-app/`。
2. 双击 `start-service.cmd`；也可在 PowerShell 中进入该目录后运行 `./pc/start.ps1`。
3. 保持终端窗口开启。
4. 浏览器打开 `http://127.0.0.1:8764`。
5. 在“手机配对”中读取本次可用的 HTTPS 地址、同步密钥和证书 SHA-256 指纹。
6. 停止服务时在终端按 `Ctrl+C`。

正式默认路径：

- 数据：`%LOCALAPPDATA%\ReceiptSync`
- 模型：`%LOCALAPPDATA%\ReceiptSync\paddle_models`
- CSV：`%LOCALAPPDATA%\ReceiptSync\exports`
- TLS：`%LOCALAPPDATA%\ReceiptSync\tls`

只测试电脑页面和 OCR 时，可双击 `test-local.cmd`。它使用 `work/pc-local-test/` 并只开放 `http://127.0.0.1:8764`。它没有 HTTPS 端口，iPhone 客户端会拒绝使用，不能拿它做真机同步测试。

Windows 防火墙只应按需要放行入站 TCP 8765。不要开放 8764。校园网 IP 会变化，不要把过去测试时出现过的 IP 写死到代码或设置说明中；总是使用当前配对页显示的地址。

## 8. 如何在 Mac 安装并测试 iPhone 端

前提：Mac 安装 Xcode 16 或更高版本；真机运行 iOS 17 或更高版本。Windows 无法签名 iPhone 应用。

1. 将整个 `work/receipt-sync-app/` 的最新源码安全地带到 Mac；保持目录结构，不要携带 Windows 运行数据、TLS 私钥或配对令牌。
2. 在 Mac 用 Xcode 打开 `ios/ReceiptSync.xcodeproj`。
3. 选择 `ReceiptSync` target，在 Signing & Capabilities 中选择用户自己的 Apple Team。
4. 把默认 `com.local.receiptsync` 改成用户账户下唯一的 Bundle Identifier。
5. 连接并信任 iPhone，选择该真机作为 Run Destination，然后 Build & Run。
6. Windows 上启动 `start-service.cmd`，确保两台设备位于同一可互访局域网。
7. 在 iPhone 应用“同步设置”中填写 Windows 配对页当时显示的完整 `https://<IP>:8765`、同步密钥和 64 位十六进制证书 SHA-256 指纹。

建议按以下顺序验收：

1. 下拉刷新，确认手机能读取当月汇总。
2. 手机新增一笔手工收入，在 Windows 九月/相应月份流水中确认出现且总收入增加。
3. 手机新增一笔手工支出，确认支出大类与结余更新。
4. 手机拍摄并上传小票，观察 `waiting/uploading/processing/review` 状态流转。
5. 在 Windows 审核具体菜品/商品、总额和大类并确认，确认手机汇总随刷新更新。
6. 在 Windows 删除该小票，手机下次同步应收到 404 并清除本地对应队列项。
7. 暂停 Windows 服务后再拍一张，确认手机保留失败队列；恢复服务后下拉刷新，确认可重试。
8. 校园 Wi-Fi 无法直连时，让 Windows 连接 iPhone 个人热点重新测试。这是网络客户端隔离问题，不应通过关闭 TLS、取消证书固定或扩大远程权限绕过。

免费 Apple ID 的开发签名通常需要定期重新安装；长期自用或分发需要合适的 Apple Developer 账号。用户已明确暂时不要打包，因此不要在未被要求时创建 `.ipa`、TestFlight 流程或发布配置。

## 9. 验证基线

截至 2026-09-03：

- 22 个 Python 自动化测试通过。
- Python 语法检查通过。
- `start.ps1` 和 `generate_certificate.ps1` 可在 Windows PowerShell 5.1 解析。
- PowerShell 5.1 生成的证书和 RSA 私钥可被 Python TLS 加载，计算出的证书 SHA-256 与保存指纹一致。
- 单 Python 进程双端口启动已验证。
- 真实局域网接口测试结果：带令牌读汇总为 200、手机手工记账为 201、带手机令牌读全量流水为 403、删除为 403、无令牌为 401、远程打开审核页为 404。
- Windows 上无法编译 Swift，因此 iOS 代码目前只做过静态审查，尚未在 Xcode 或 iPhone 真机完成验收。

在 Windows 修改电脑端后至少运行：

```powershell
& "C:\Users\15610\Documents\ChatGPT\DesktopAssistant\.venv-paddle\Scripts\python.exe" -m unittest discover -s pc -p "test_*.py" -v
```

在 Mac 修改 iOS 端后至少完成：

- Xcode Debug build 无错误和新增警告。
- iOS 17+ 模拟器进行基础界面检查；相机和局域网同步必须用真机检查。
- 上述端到端验收序列，尤其是证书不匹配时拒绝连接、离线队列恢复和电脑删除后的 404 清理。

不要用正式 `%LOCALAPPDATA%\ReceiptSync` 或现有 `work/pc-local-test` 做破坏性自动化测试。新测试使用临时数据目录，但模型缓存仍指向持久共享缓存。

## 10. 已修复的高风险问题

继续开发时应保留相应回归测试：

- 小票 ID 路径穿越和越界图片删除。
- 手机令牌权限过大，可读取 OCR 原文、全量账目或执行管理操作。
- 手工流水 ID 覆盖小票流水。
- 删除小票与 OCR worker 完成之间的竞态导致记录复活。
- 畸形 HTTP body 影响持久连接中的后续请求。
- CSV 非原子写入。
- 首次启动时配置/令牌初始化竞态。
- Windows PowerShell 5.1 证书私钥导出不兼容。
- 双进程启动方式异常退出后留下后台服务；当前正式架构为单进程双监听器。
- 本地未知商户旧值的兼容迁移。

## 11. 尚未完成和建议下一步

当前最重要的下一步是 Mac/Xcode 真机构建与端到端测试，而不是继续扩展功能。

尚未完成：

- iOS 工程尚未在 Xcode 16 实际编译，也未在真机测试相机、Keychain、证书固定、本地网络权限和后台刷新。
- 尚未接入 DeepSeek 或其他第三方商品分类服务。
- 尚未实现 Passkey、外出同步、云中转、Tailscale 或 VPN。
- 尚未打包或发布。

后续若要接入 AI 分类，先明确数据是否允许离开电脑、供应商密钥如何保存、失败时的本地回退、费用和可审计性。默认应保持本地 PaddleOCR 和人工审核；不要让模型分类结果绕过人工确认，也不要把服务费或舍入重新拆成虚假商品。

后续若要接入 Passkey，优先限定为“首次配对/管理授权”，并保留短权限设备凭证执行后台同步。只有在有稳定、可信的 HTTPS 域名和正确的 Associated Domains/WebAuthn RP 配置后再实现。

## 12. Agent 工作约束

- 先读本文件和 `README.md`，再改代码。
- 保持 HKD、香港月份、人工确认后入账、电脑计算汇总这四个核心口径。
- 不要在 UI 中泄露同步令牌、私钥、OCR 原文或全量账目给远程调用方。
- 不要因为调试方便而允许 HTTP 局域网同步、跳过证书固定或开放电脑审核端口。
- 不要覆盖用户现有数据库或小票图片；数据库 schema 变更必须向后兼容并有测试。
- 修改解析器时添加真实版式的最小化 OCR 行回归测试，覆盖坐标关系和金额合计。
- 修改 API 权限时更新 `test_phone_token_has_only_required_endpoints` 一类安全边界测试。
- 修改 iOS 同步状态时同时检查离线重试、重复上传幂等、远端删除 404 和本地图片生命周期。
- 除非用户明确要求，暂不打包、发布、上传云端或引入第三方数据处理。
