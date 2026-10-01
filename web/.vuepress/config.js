const { mdEnhancePlugin } = require("vuepress-plugin-md-enhance");

module.exports = {
  plugins: [
    mdEnhancePlugin({
      // 启用容器功能
      container: true,
      // 如需其他功能，也可一并启用
      tasklist: true,
      tabs: true,
      mermaid: true,
      // 更多功能...
    }),
  ],
};