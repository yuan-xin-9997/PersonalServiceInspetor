## 架构要求

1. 优先基于 Python 语言开发，后端架构使用 Fastapi、前端架构使用 Vue。
2. 数据库优先使用轻量数据库 SQLite，如果要开发的系统不适合 SQLite，需要给出理由并经过审批才能更换。
3. 如果涉及到网页抓取，优先使用集中的网页抓取服务，API Key 是966f9d9f573e6efcb889b5fe9bfe48b6c528e4a08ebddc3ffcbd5c49406a9fa9，服务地址：[http://192.168.0.111:33333/](http://192.168.0.111:33333/health/ready)，健康检查http://192.168.0.111:33333/health/ready，接口文档[WebFetch Service - Swagger UI](http://192.168.0.111:33333/docs)，README[web_fetch/README.md at main · yuan-xin-9997/web_fetch · GitHub](https://github.com/yuan-xin-9997/web_fetch/blob/main/README.md)
4. 如果涉及到翻译需求，优先使用我在NAS部署的翻译服务（基于 ollama），API Key 是48689f5f386ea97ea7b6bb32deb6c1f3700d1a709104ecc1b1619406ac3f9c4a，服务地址：[http://192.168.0.100:11880/](http://192.168.0.100:11880/docs)，接口文档：http://192.168.0.100:11880/docs，健康检查：http://192.168.0.100:11880/health。如果 IP 访问失败，尝试将 IP 端口替换为公网域名https://translate.yuan-xin.top/
5. 如果涉及到 OCR 需求，优先使用我在NAS部署的 OCR 服务（基于 ollama），API Key 是c20d02024ae9402f8963bfad819213f0701d51894806b1cc638b3d7a5434da06，服务地址：http://192.168.0.100:11980/，接口文档：http://192.168.0.100:11980/docs，健康检查：http://192.168.0.100:11980/health。如果 IP 访问失败，尝试将 IP 端口替换为公网域名[https://ocr.yuan-xin.top/](https://translate.yuan-xin.top/)

## 基本模块或功能要求

以下是系统必须包含的基本模块，哪怕用户在需求中没有明确提到。

1. 登录功能：系统支持不同用户登录，可以登录的用户名和密码维护在 password.txt 中。
2. 权限管理模块：用于维护可登录本系统的用户信息，包含用户名、角色（管理员、普通用户）、可访问的页面。
3. 系统配置模块：显示当前系统的配置，包括配置在配置文件中的配置。
4. 任务中心模块：显示当前系统的任务列表、任务日志、任务状态等

## 系统 UI 界面要求

1. 在系统左下角要显示当前登录用户是谁，还要有退出按钮可以退出系统，另外还要显示系统当前的版本号（暂时使用提交到 Github 生成的数字编号作为版本号）。

## 代码目录结构要求

项目大体按照如下目录结构开发：

src

├── app # 前后端代码目录

├── config # 配置文件目录

│   └── app.json # 系统主配置文件，JSON 格式

├── data # 数据目录

│   ├── app.sqlite3 # SQLite 数据文件

│   └── password.txt # 用户密码信息

├── JenkinsConfig # 存放 Jenkins 相关文件

│ ├── Jenkinsfile # Jenkins 流水线文件

├── tests # 测试脚本

├── logs # 日志目录

│   ├── app.log # 当天的日志

│   ├── app.xxxx-xx-xx.log # 历史日志，按天自动切割

│   └── server.pid # 当前系统的主进程 PID

├── README.md # 自述文件

├── start.ps1 # Windows 启动系统脚本

├── start.sh # Linux 启动系统脚本

├── status.ps1 # Windows 显示系统状态脚本

├── status.sh # Linux 显示系统状态脚本

├── stop.ps1 # Windows 停止系统脚本

└── stop.sh # Linux 停止系统脚本

## 开发规范

1. 禁止在代码中硬编码任何环境相关信息，比如 IP、端口、用户名、密码、绝对路径等信息。这些必须可配置
2. 系统显示的时间如果不是北京时间，需要在原始时间的基础上显示北京时间
3. 若有下载的文件需要按年份/月份/天保存在 data 目录
4. 项目 .gitignore 文件要包含 logs 目录，但是不能包含 data 目录

## 测试要求

1. 所有功能必须做基本的单元测试、冒烟测试等，测试必须都通过

## 部署要求

1. 首次部署的时候，需要创建 data 目录，并创建 password.txt，添加附件提到的默认内容。后续增量部署则不需要重复创建 data 目录
2. 在完成自测之后，交付给我之前，需要将项目整合到 Jenkins 中，参照“生成Jenkinsfile的提示词.md”执行部署
3. 如果部署在Linux系统上，则还需要支持systemd的方式进行系统的启停、状态检查。

## 文档要求

1. README.md 文件需要包含系统介绍、页面介绍、配置文件说明、部署方式、运维方式、访问方式等章节，且需要及时更新
2. 需求规格说明书、设计说明书需要及时更新

## 需求新增或变更的要求

1. 若用户有需求新增或变更，在开发自测完后，需要根据情况更新需求规格说明书、设计说明书、README.md、Jenkinsfile 等文件
2. 代码提交并推送到到 Github 的 main 分支，成功之后，需要使用 API 的方式触发 Jenkins 的手工构建，Jenkins 的地址、用户名和密码见文档“生成Jenkinsfile的提示词.md”
3. 在部署的时候注意不要把 config/app.json 给覆盖了，因为有可能在第一次部署之后 app.json 文件里面的配置被修改了

## 附件

### password.txt 默认内容

```
# 格式: username:password:role  (role 取值: admin | user)
# admin 默认拥有所有页面权限；user 的可见页面由管理员在权限管理页配置。
# 修改本文件后，新用户在下次登录时会自动同步到数据库。
admin:admin123:admin
```
