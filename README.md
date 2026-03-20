# AstrBot MAA 远程控制插件 (astrbot_plugin_maa)

通过 AstrBot 远程控制 [MAA Assistant Arknights](https://maa.plus/)（一款明日方舟游戏小助手）

![Moe Counter](https://count.getloli.com/@astrbot_plugin_maa?name=astrbot_plugin_maa&theme=capoo-2&padding=7&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)

## 特性

-   **远程控制**: 支持通过 QQ 等消息平台远程启动 MAA 任务
-   **多任务队列**: 支持一次下发多个任务，按顺序执行
-   **多设备支持**: 一个用户可以绑定并控制多个 MAA 实例（如多个模拟器）
-   **实时反馈**: 任务完成时自动发送通知，并可选自动发送截图

## 快速开始

### 1. 安装插件

在 AstrBot 中安装本插件

### 2. 获取设备 ID

1.  打开 MAA 客户端
2.  进入 **设置** -> **远程控制**
3.  找到并复制 **设备标识符** (一段长字符串)，如果没有的话先点右边的重新生成再复制


### 3. 绑定设备

向 bot 发送以下指令进行绑定：
```
/maa bind <你的设备标识符>
```
成功后，bot 会告知你需要填入 MAA 的配置信息

![示例](img/maa_bind.jpg)

### 4. 配置 MAA

在 MAA 客户端的 **设置** -> **远程控制** 中配置：

-   **获取任务端点**: `http(s)://<你的设备地址>:<端口号>/maa/getTask`
-   **汇报状态端点**: `http(s)://<你的设备地址>:<端口号>/maa/reportStatus`
-   **用户标识符**: 填入你在绑定成功后 bot 告知你的那一串字符串，如果是 QQ 的话应该是 QQ 号

**默认端口号：`2828`**

> [!WARNING]
> 如果该端点为 http 协议，MAA 会在每次连接时发出不安全警告。**在公网部署明文传输服务是一种非常不推荐且危险的行为，仅供测试使用**

![MAA配置示例](img/MAA配置示例.jpg)

## 指令

### 基础指令

-   `/maa bind <ID> [别名]`: 绑定 MAA 设备（支持多次绑定不同设备，别名可不填）
-   `/maa unbind [ID|别名]`: 解绑指定的设备（如果是唯一设备可不填）
-   `/maa rename <旧别名或ID> <新别名>`: 重命名已绑定设备的别名
-   `/maa list`: 列出当前绑定的所有设备及状态
-   `/maa switch <ID|别名>`: 在多个绑定的设备间切换当前控制对象
-   `/maa status`: 查看当前操作设备在线状态及待执行任务
-   `/maa screenshot` (或 `/maa ss`): 立即获取当前设备的模拟器截图
-   `/maa stop`: 停止当前正在执行的任务
-   `/maa clear`: 清空任务队列
-   `/maa heartbeat`: 发送心跳检测

### 执行任务

**`/maa start <任务1,任务2,...>`**

支持的任务别名：

| 任务类型 | 可选别名 |
| :--- | :--- |
| **所有任务** | `ALL` |
| **基建换班** | `Base`, `基建`, `基建换班` |
| **开始唤醒** | `WakeUp`, `开始唤醒` |
| **刷理智** | `Combat`, `刷理智` |
| **自动公招** | `Recruiting`, `公招`, `自动公招` |
| **信用购物** | `Mall`, `信用`, `获取信用及购物` |
| **领取奖励** | `Mission`, `领取奖励` |
| **自动肉鸽** | `AutoRoguelike`, `肉鸽`, `自动肉鸽` |
| **生息演算** | `Reclamation`, `生息演算` |

**示例:**
-   `/maa start ALL` (一键长草)
-   `/maa start 刷理智` (单个任务)
-   `/maa start 开始唤醒,刷理智,信用,领取奖励` (按序执行多个任务)

> **也可以使用 `/maa linkstart` 来执行一键长草喵**

![示例](img/maa_start.jpg)

## 插件配置

在 AstrBot 管理面板或 `config.yaml` 中可配置：

-  `http_host`: HTTP 服务监听地址 (默认 `0.0.0.0`)
-  `http_port`: HTTP 服务监听端口 (默认 `2828`，因为2月8日是[帕拉斯](https://prts.wiki/w/%E5%B8%95%E6%8B%89%E6%96%AF#%E5%B9%B2%E5%91%98%E6%A1%A3%E6%A1%88)干员，也就是 MAA 吉祥物的生日)
-  `auto_screenshot`: 任务完成后是否自动发送截图 (默认 `true`)

> [!WARNING]
> 如果你需要发送截图功能，请务必注意你的端点可接受的最大请求大小，因为截图可能会有数十MB，会超过一般网关的默认大小限制

## 更新日志

参见 [CHANGELOG.md](CHANGELOG.md)

## 开源协议

[AGPL-3.0](LICENSE)

## 参考资料

- [MAA Assistant Arknights](https://maa.plus/)
- [远程控制协议 | MAA 文档站](https://docs.maa.plus/zh-cn/protocol/remote-control-schema.html)
- [AstrBot 插件模板](https://github.com/Soulter/helloworld)
