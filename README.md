# 智能问数 Agent · 交互演示

一个根据项目复盘重建的 NL2SQL 交互演示：自然语言提问 → Schema 召回 → 生成 SQL → Query Checking → 执行 → 总结。

- 线上：https://tomo-efficient.github.io/smart-query-agent/
- 个人网站：https://tomo-efficient.github.io/resume/

## 说明

- 页面内的数据、表结构和指标口径全部为模拟，不对应招商金科真实业务数据。
- 这是公开演示，不是生产系统；只允许查询，写操作会在校验阶段被拦截。
- 单文件静态页，无构建、无依赖，直接打开 `index.html` 即可。

## 内容

- 5 组预置问题：渠道保费环比、互联网退保率、件均保费、渠道件均、险种保费占比。
- 自由输入按关键词匹配预置问题；未命中会提示。
- 识别删除、更新、drop 等写操作，展示「已拦截」。
- 结果包含结论、SQL、查询表格、分布条、安全校验和检索依据。
- 支持 SQL 复制、移动端布局和 `prefers-reduced-motion`。

## 本地预览

```bash
python3 -m http.server 8080
# 打开 http://localhost:8080/
```
