# 高德 MCP 配置参考

## TRAE 中高德 MCP 的配置形态

高德官方提供的是远程 StreamableHTTP 服务，配置只需一个 URL：

```json
{
  "mcpServers": {
    "amap-maps-streamableHTTP": {
      "url": "https://mcp.amap.com/mcp?key=<你的Web服务Key>"
    }
  }
}
```

## 配置文件路径（按产品/版本）

| 产品 | 路径 |
|---|---|
| TRAE CN（IDE 国内版） | `~/Library/Application Support/Trae CN/User/mcp.json` |
| TRAE SOLO CN | `~/Library/Application Support/TRAE SOLO CN/User/mcp.json` |
| TRAE 国际版 | `~/Library/Application Support/Trae/User/mcp.json` |

读取该文件即可：
- 判断用户是否已安装高德 MCP；
- 从 URL 的 `?key=` 直接解析出可用 Key（无需再问用户）。

## 用户没有 Key 时的引导话术要点

1. 打开高德开放平台控制台：https://console.amap.com
2. 「应用管理」→「创建新应用」（应用名随意）
3. 在应用上「添加 Key」，**服务平台选择「Web服务」**
   （本技能的 REST 调用与 MCP 共用此类型 Key）
4. 复制生成的 Key：
   - 填入 MCP URL 完成 MCP 添加；
   - 调用本技能脚本时用 `--key` 或 `AMAP_KEY` 环境变量提供。

## 为什么本技能不直接调用 MCP 工具

高德 MCP 工具（text/around/detail 等）封装时固定 `extensions=base`，
且未暴露 extensions 参数，因此：
- 拿不到电话 tel、特色菜 tag、biz_ext（人均/评分/营业时间）；
- 同样受 200 条翻页上限影响。

所以本技能的策略是：**借 MCP 配置获得 Key，用 Key 直接调 REST API**
（脚本中统一传 `extensions=all`，并用四叉树突破 200 条）。
MCP 本身可以保留，用户日常地图类问答仍可使用。

## Key 安全

- 不要把 Key 写进任何会提交到 Git 的文件；
- 不要在聊天/日志中完整回显 Key（需要时只显示前 3 后 3 位）；
- Key 一旦疑似泄露，引导用户在控制台删除并重新生成。
