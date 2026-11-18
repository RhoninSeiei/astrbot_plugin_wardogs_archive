# AI workflow · AI 工具工作流

Read README.md, docs/OPERATIONS.md and _conf_schema.json. main.py registers the chat handlers. wardogs/io.py is the client; wardogs/admin_auth.py checks the sender and group scope.

Reproduce permission and read-query paths using a fake backend and fresh temporary data. Keep model output as a proposal. Application code must validate the actor, target, expiry and confirmation before dispatch. Keep credentials, real IDs and raw chat out of prompts and fixtures.

运维从只读状态和既有操作回执入手。真实处罚、群消息、配置写入和条款确认由操作者授权。结果未知时先查原操作号。项目已归档，后续开发请在自己的分支或派生仓库进行。
