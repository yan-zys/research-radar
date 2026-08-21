# Cloudflare Worker：让反馈提交脱离本机

日报网页（GitHub Pages）上的 相关/不相干/已读/收藏、加关键词、贴文献
三个功能，原本依赖本机 `http://127.0.0.1:8710` 的反馈服务（电脑没开机时
提交会失败）。部署这个 Worker 后，网页点击 → Worker → 直写 GitHub 仓库，
全程在云端完成，不再依赖任何本机进程。

工作原理：Worker 接收网页请求后，通过 GitHub Contents API 把记录追加进
仓库的 `input/user_feedback/YYYY-MM-DD.jsonl` 和
`input/keyword_requests/YYYY-MM-DD.jsonl`（日期按北京时间），与本机
`feedback/server.py` 写的格式完全一致；每日 workflow 的 learning /
apply_page_requests 步骤照常消费，无需任何改动。

## 部署（约 10 分钟，一次性）

前置：一个 Cloudflare 账号（免费，dash.cloudflare.com 注册）。

方式 A（命令行，机器上有 Node）：

```bash
npm install -g wrangler
wrangler login                      # 浏览器跳转授权
cd cloudflare
# 编辑 wrangler.toml，把 YOUR_ACCOUNT_ID 换成你的 account_id
# （dash 首页右侧栏可见，或 wrangler whoami 查看）
wrangler secret put GH_TOKEN        # 粘贴 GitHub fine-grained PAT：
                                    # 仓库权限只勾 research-radar 的
                                    # Contents: Read and write
wrangler secret put APP_KEY         # 随便设一段口令，例如 rr-fb-x4k9
wrangler deploy                     # 输出形如：
# https://research-radar-feedback.<你的子域>.workers.dev
```

方式 B（纯网页，无 Node）：Cloudflare dash → Workers & Pages →
Create Worker → Deploy → Edit Code，把 `worker.js` 全文粘贴进去保存；
然后在 Worker 的 Settings → Variables and Secrets 里添加两个 Secret：
`GH_TOKEN` 和 `APP_KEY`。

## 部署完成后

把 Worker 的 URL 和 APP_KEY 发给维护者（或自己改），替换
`email/page_template.html` 中的两处占位符并推送：

- `https://WORKER_URL_PLACEHOLDER.workers.dev` → 你的 Worker URL（不带路径）
- `APP_KEY_PLACEHOLDER` → 你设置的 APP_KEY

页面 JS 会优先请求 Worker，失败时自动回退本机 8710，两份写入的是同一
格式、同一个仓库，不会互相冲突。

## 安全说明

- `GH_TOKEN` 只存在 Cloudflare 的 Secret 存储里，网页和仓库中均不可见；
  权限收敛到单仓库 contents:write，泄露面最小。
- `APP_KEY` 会出现在公开页面的 JS 里（GitHub Pages 静态站没有真正的
  服务端密钥），作用只是挡住无关路人乱点；即便如此，被写入的也只是
  feedback jsonl 行，内容受长度/白名单校验，每日 workflow 才消费。
- 想彻底收紧：可以在 Worker 里再加 Turnstile 人机校验，后续需要再做。
