# 量化系统
这是一个量化交易系统，使用Python实现。该系统基于TigerOpen API，通过接收Tradingview传递的交易信号来执行买卖单和更新本地数据。运行该系统需要挂载该文件在服务器中。


## 提示

- 作者没有购买tiger实时数据，所以无法检测盘中盘后状态。不过我曲线救国，编写了根据服务器时间进入不同交易状态的method并每5分钟校对一次，可能需要根据夏令时重新校准。如果需要实时数据，
- 还是同一个问题，没有实时数据的情况下，实现获取实时价格改单的方式是每分钟使用TradingView发送价格信号，在TradingView通过webhook每分钟发送一次需要监控的标的价格变动。对于初级付费用户，最多可以同时建立20个交易信号，20个价格信号。也就是说，这个系统最多一次自动化交易20个个股。
- 这是作者在大一时写的学习项目，虽然系统已经经过6个月压力测试，且可以确定盘中交易99.9%不会发生问题，但如果需要进入生产环境使用，请确保检查所有细节并了解每一个函数和变量的定义。

- 目前正在编写一套半自动Python图形交易系统，用于日内交易，功能包括分型识别/预设形态选股/自动追踪调整挂买价格/实时新闻监控 + 大模型给出决策建议等。

## 系统特点


低中频交易：支持分钟级别交易，自动挂单/改单/撤单。高于每分钟/次的情况下，容易出现问题。Python不支持并发，如需高频交易，需更改为其他代码。

最佳部署位置：建议将服务器布置在美国华盛顿周边，以优化美股交易的延迟。

灵活集成：如需跨过Tradingview，可考虑使用vector bt或其他开源框架作为策略端和回测端，但需要妥善解决实时数据问题。

自动化功能：系统具备自动下单、改单、撤单、监控和通知功能。当前功能涉及盘后交易，改单基于成交量决定，如果不需要盘后单，可以修改place_order函数禁用该功能。


## 功能特性


市场状态管理：自动更新市场状态，考虑夏令时和冬令时。

订单管理：处理并发数据，管理订单队列，确保订单执行和修改的正确性。

实时数据处理：集成TigerOpen API以获取实时市场数据和订单状态更新。

日志记录和邮件通知：详细的日志记录和关键事件的邮件通知。

CSV记录：记录所有交易和持仓到CSV文件，以供分析和可视化。


## 先决条件
```
Python 3.7+
所需库：Flask、asyncio、logging、aiofiles、pytz、smtplib、TigerOpen API SDK
```
## 安装
```
git clone https://github.com/Joezhou1211/QuantSystem.git
cd QuantSystem
```
## 设置TigerOpen API

从老虎量化获取私钥 https://quant.itigerup.com/openapi/en/python/overview/introduction.html

将私钥放置在适当的目录（如/home/admin/key.pem）

## 软件截图
<img src="https://github.com/Joezhou1211/QuantSystem/assets/121386280/1788d333-5814-4028-9659-d51f3ab9c0b9" width="400">

<img src="https://github.com/Joezhou1211/QuantSystem/assets/121386280/ce2e1943-d2a8-4c08-bd47-842ae9f9b9db" width="600">


