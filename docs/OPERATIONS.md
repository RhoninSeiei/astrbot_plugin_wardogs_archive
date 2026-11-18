# Plugin operations · 插件运维

- Keep query, report execution and admin tokens in separate files. Use the corresponding backend credential. Keep the API on a private network.
- Set `official_query_scopes` and `official_admin_scopes` separately. Each entry uses `platform_instance|AppID|group_openid`. Match backend actor scope with `wardogs/admin_history.py`.
- QQ 官方机器人使用 OpenID。额外授权成员先发送 `@机器人 我的管理身份`，由管理者核对真实 QQ 后填写 QQ 名单及精确身份绑定。撤销名单或绑定后，后续确认会重新校验。
- `回溯` and `回溯5分钟` read the event cache by archive receipt time. `查 <昵称或SteamID> 50` reads retained personal history. Enable manual management only after validating the dedicated admin scope and operation receipt flow.
- 回溯无数据时，依次检查游戏推送状态、接收端最后入库时间、缓存游标与查询时间窗。平台条款由服务器操作者决定是否接受。
- Before upgrading, back up the plugin's config and database privately. On rollback preserve current audit records and check an uncertain operation before retrying.
- In an AstrBot environment run `PYTHONPATH=tests:.:$PYTHONPATH python -m unittest discover -s tests`. Tests use temporary data and loopback substitutes.
