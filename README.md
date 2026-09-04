# 小票同步

这是一个本地优先的小票录入应用：iPhone 负责拍照、离线排队和查看汇总；Windows 电脑负责 PaddleOCR、人工审核、账目保存及按大类汇总。

## 当前功能

- iPhone 拍摄小票并保存到本地队列。
- 拍照后立即同步；电脑不可达时自动保留，回到同一局域网后重试。
- iOS 后台刷新会机会性重试，但实际时间由系统决定，不能保证精确间隔。
- Windows 使用现有 PaddleOCR 3.7.0 识别，原图、OCR 原文和结构化结果保存在本机。
- 小票必须在电脑审核页确认后，才会计入手机汇总。
- 手机显示月度总收入、总支出、结余、收入大类、支出大类和待处理小票数。
- 电脑审核页按香港月份显示收支流水；流水标明日期、类型、大类、内容、金额和来源。
- 电脑审核页可以删除整张小票（连同流水、商品明细和原始图片）或删除手工单笔录入；删除前会二次确认，并自动重导出 CSV。
- 电脑删除小票后，iPhone 会在下次同步时移除对应的本地队列记录。
- 手机和电脑均可手工登记收入或支出。
- 确认后的数据自动导出为 UTF-8 CSV，便于 Excel 导入。

餐饮口径：有价菜品和饮品逐项记录；服务费、茶芥、附加费和收款舍入合并成一条“餐饮非菜品（合并）”；`Welcome`、“不用加配”等 HK$0 项目不录入。

## Windows 电脑端

要求：Windows 10/11，以及已经安装 PaddleOCR 的 Python。本机默认使用：

```text
C:\Users\15610\Documents\ChatGPT\DesktopAssistant\.venv-paddle\Scripts\python.exe
```

在 PowerShell 中运行：

```powershell
.\pc\start.ps1
```

也可以直接双击项目根目录的 `start-service.cmd`。服务运行期间请保持窗口开启；按 `Ctrl+C` 可停止服务。

启动后：

- 电脑审核页：`http://127.0.0.1:8764`
- iPhone 加密同步端口：`8765`
- 数据目录：`%LOCALAPPDATA%\ReceiptSync`
- Paddle 模型：`%LOCALAPPDATA%\ReceiptSync\paddle_models`
- CSV：`%LOCALAPPDATA%\ReceiptSync\exports`

首次识别会下载固定的 `PP-OCRv6_medium_det` 和 `PP-OCRv6_medium_rec` 模型。正式服务和临时测试的数据目录彼此独立，但统一复用上述持久、可写的模型缓存；删除测试数据不会删除模型。只有手工删除模型目录或以后明确升级模型版本时才会重新下载。

在电脑审核页点击“手机配对”，可查看服务器地址、同步密钥和证书 SHA-256 指纹。

## iPhone 端

必须在 Mac 上完成签名和安装：

1. 使用 Xcode 16 或更高版本打开 `ios/ReceiptSync.xcodeproj`。
2. 选择 `ReceiptSync` Target，在 Signing & Capabilities 中选择自己的 Apple Team。
3. 将 Bundle Identifier 改为自己的唯一值。
4. 连接 iPhone，选择该设备并运行。最低系统版本为 iOS 17。
5. 在应用“同步设置”中填写电脑审核页显示的 HTTPS 地址、同步密钥和证书指纹。

Windows 无法完成 iOS 签名或生成可直接安装的 `.ipa`。免费 Apple ID 的个人签名通常需要定期重新安装；长期自用或分发需要 Apple Developer 账号。

## 大学公共 Wi-Fi

公共 Wi-Fi 上只有 HTTPS 同步端口对局域网开放；电脑审核页只绑定 `127.0.0.1`，其他设备无法访问。

安全措施：

- 电脑首次启动生成 3072 位 RSA 本地证书。
- iPhone 固定核对证书 SHA-256 指纹，不接受其他证书，避免中间人冒充电脑。
- 192 位随机同步密钥保存在 iPhone 钥匙串中，并通过 HTTPS 发送。
- 未带正确密钥的远程请求会被拒绝；连续失败会触发速率限制。
- 手机密钥只能上传小票、查询已知小票状态、读取月度汇总和手工记账，不能列出全部流水、读取 OCR 原文、审核、重新识别或删除记录。
- 审核页、配对信息和删除接口只接受本机回环地址，且会拒绝伪造的 Host 请求。
- 单张图片限制为 12 MB，只接受 JPEG/PNG；汇总仅统计人工确认记录。
- Windows 防火墙只需要放行 TCP `8765`，不要开放审核端口 `8764` 或其他端口。

当前直连方案不使用 Passkey。Passkey 需要稳定的 HTTPS 域名和关联域，无法可靠绑定校园网中会变化的电脑 IP；它也不适合需要后台自动执行的每次同步。后续如加入固定域名或中转服务，可用 Passkey 完成首次配对或授权电脑端管理操作，再签发上述受限设备凭证。

部分校园网启用了客户端隔离，即使手机和电脑连接同一 Wi-Fi 也不能互相访问。此时应用会保留手机队列，但无法直接同步。在暂不使用云中转或 VPN 的前提下，可让电脑连接 iPhone 个人热点，形成只属于这两台设备的局域网。

## 数据口径

- `总收入`：选定月份内所有已确认收入之和。
- `总支出`：选定月份内所有已确认支出之和。
- `结余`：总收入减总支出。
- 大类汇总：由电脑数据库计算并返回手机，手机不自行重算。
- 待审核或识别失败的小票不进入总收支。

## 目录

```text
pc/
  receipt_sync_server.py   HTTPS、SQLite、汇总和审核服务
  receipt_parser.py        小票金额与餐饮分项规则
  start.ps1                同时启动本机审核页和手机 HTTPS 接口
  generate_certificate.ps1
  web/index.html           电脑审核页面
ios/
  ReceiptSync.xcodeproj
  ReceiptSync/             SwiftUI iPhone 客户端
```

当前版本未接入 DeepSeek，也未实现外出访问、云端中转或 VPN。OCR 结果会保留 AI 分类字段的扩展空间，但敏感小票内容不会自动发送给第三方。
