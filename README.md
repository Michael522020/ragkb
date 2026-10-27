# RAGKB

一个可以本地直接跑起来的 RAG 知识库。上传 txt、Markdown 或 PDF，按知识库做混合检索，再用流式接口返回答案和依据。

产品目标参考公开项目 [9Z-jiuzhang/0716RAGPJ](https://github.com/9Z-jiuzhang/0716RAGPJ)（MIT）。本仓库是独立重写，不包含该项目的源码。

## 能力

- 账号、管理员 / 成员，以及部门
- 知识库三种可见范围：公开、部门、私有
- 文档解析、分段、向量化；失败会记在文档上
- 检索把向量相似度和词法重叠用 RRF 融在一起
- 问题与 FAQ 完全一致时直接返回答案
- 没有足够依据时明确拒答，不编造来源
- 配置了 OpenAI 兼容接口时流式生成；否则按资料摘录回答
- 问答会话可续聊

## 架构

```text
浏览器
  ├─ /            问答页
  └─ /admin       管理页
        │
        ▼
FastAPI  /api/v1
  ├─ 身份与部门
  ├─ 知识库、文档、FAQ
  └─ POST /qa/ask   SSE
        │
        ▼
SQLite（默认）
  users / departments / knowledge_bases / documents / chunks / faqs / qa_sessions
```

向量默认存在分段记录里，查询时在进程内计算余弦相似度，并和中英文词法分数做 RRF。设置 `EMBEDDING_API_KEY` 后，入库和查询改为调用远程嵌入接口。没有 `LLM_API_KEY` 时不请求聊天模型。

部门决定“能看到哪个库”，角色决定“能不能建公开库、管理部门和用户”。访客只能读取公开库并提问。

## 本地运行

需要 Python 3.11+。

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --port 8080
```

- 问答：<http://localhost:8080/>
- 管理：<http://localhost:8080/admin>
- 接口文档：<http://localhost:8080/docs>

初始管理员是 `admin` / `Admin123!`。首次启动会写入一个公开的示例知识库。生产环境请修改 `JWT_SECRET` 和 `ADMIN_PASSWORD`，见 `.env.example`。

```bash
docker compose up --build
```

容器同样监听 8080。在项目目录放 `.env` 后，Compose 会用其中的变量替换端口旁的配置。

## 接入模型

`.env` 里填写 OpenAI 兼容地址即可，例如通义、DeepSeek 或 OpenAI：

```bash
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=sk-...
LLM_MODEL=gpt-4o-mini

EMBEDDING_BASE_URL=https://api.openai.com/v1
EMBEDDING_API_KEY=sk-...
EMBEDDING_MODEL=text-embedding-3-small
```

更换嵌入模型后，对已有文档调用 `POST /api/v1/documents/{id}/reprocess`。

## 接口摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/auth/register` | 注册成员 |
| POST | `/api/v1/auth/login` | 登录，返回 Bearer token |
| GET | `/api/v1/auth/me` | 当前用户 |
| POST | `/api/v1/auth/password` | 修改密码 |
| GET/POST | `/api/v1/departments` | 部门；创建需管理员 |
| GET/PATCH | `/api/v1/users` | 用户；管理员 |
| GET/POST | `/api/v1/knowledge-bases` | 知识库；GET 按身份过滤 |
| POST | `/api/v1/knowledge-bases/{id}/documents` | 上传文档 |
| POST | `/api/v1/knowledge-bases/{id}/faqs` | 精确问题 |
| POST | `/api/v1/qa/ask` | SSE：`session`、`citations`、`chunk`、`done` |

除健康检查、注册、登录和公开库读取外，请求头带 `Authorization: Bearer <token>`。

## 测试

```bash
pytest -q
```

## 这版没有带上的部分

参考项目是一套完整的企业平台：PostgreSQL 全文索引、Chroma、Redis、MinIO、Nginx、Prometheus/Grafana、Langfuse、RAGAS、快照回滚、密级和多级缓存。那些适合作为后续替换的存储和观测层。这一版把同一条主路径收成单进程，方便先跑通再换基础设施。

## 许可

MIT
