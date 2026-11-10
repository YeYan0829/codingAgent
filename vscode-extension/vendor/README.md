# 内置 Markdown 渲染器

`markdown-it-15.0.3.min.js` 是官方 npm 发行包的原始浏览器 UMD 文件：

- 包：[markdown-it 15.0.3](https://registry.npmjs.org/markdown-it/-/markdown-it-15.0.3.tgz)
- 包内路径：`package/dist/browser/markdown-it.umd.min.js`
- npm integrity：`sha512-+Az7Ojz3iRGu8KYMvJ6xVhvrxP3eE+vEtJX75Ow88XBpcQNV/0lGX5ZynIIBftREo4DC2jsveAMDaeltmm8QvQ==`
- 原文件 SHA-256：`391f2aa638e75e6c7859d0a5c5650ef0956722a45998f17e05b83bc655778e1e`
- 主许可证：[MIT](markdown-it-LICENSE.txt)。浏览器包包含 mdurl、uc.micro、entities、linkify-it、punycode.js；对应版权与许可原文保留在 [THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt)。未加入 CLI 依赖 argparse。

扩展将此本地资源注入带 nonce 的 Webview 脚本，不访问 CDN，不需要用户执行 npm install。
`markdown.js` 只配置渲染规则：禁用原始 HTML 和远程图片，链接允许 HTTP(S)、mailto 和页内锚点，代码文本转义。
Markdown 语法及选项依据 [官方用法](https://github.com/markdown-it/markdown-it/blob/master/docs/usage.md)。

更新时下载固定版本发行包、检查 npm integrity、替换原始 UMD 和许可证，再运行完整扩展测试。
当前未捆绑 source map；开发工具无法据此回溯上游源文件。运行时不需要 source map。
