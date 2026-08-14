# 页面导航管理

这是一个使用 Python 标准库 `http.server` 构建的多页面入口管理器。页面会读取
`config.json`，支持新增、修改和删除入口，并将更改持久化写回配置文件。每个入口还
可以设置可选的 `help` 帮助内容。

## 启动

```powershell
python web_server.py
```

然后访问 <http://127.0.0.1:8080>。点击入口会在新标签页打开对应地址。
