这是一个量化交易系统，使用Python实现。该系统基于TigerOpen API，通过接收Tradingview传递的交易信号来执行买卖单和更新本地数据。运行该系统需要挂载该文件在服务器中。


***系统特点***


高频交易：系统已达到毫秒级，可以执行中高频交易，但如果每秒操作大于10次，可能会出现未知问题，如券商接口限制等。

最佳部署位置：建议将服务器布置在美国华盛顿周边，以优化美股交易的延迟。

灵活集成：如需跨过Tradingview或实现微秒级交易，可考虑使用vector bt或其他开源框架作为策略端，但需要妥善解决实时数据问题。

自动化功能：系统具备自动下单、改单、撤单、监控和通知功能。当前功能涉及盘后交易，改单基于成交量决定，如果不需要盘后单，可以修改place_order函数禁用该功能。


***功能特性***


市场状态管理：自动更新市场状态，考虑夏令时和冬令时。

订单管理：处理并发数据，管理订单队列，确保订单执行和修改的正确性。

实时数据处理：集成TigerOpen API以获取实时市场数据和订单状态更新。

日志记录和邮件通知：详细的日志记录和关键事件的邮件通知。

CSV记录：记录所有交易和持仓到CSV文件，以供将来分析和可视化。


***先决条件***


Python 3.7+
所需库：Flask、asyncio、logging、aiofiles、pytz、smtplib、TigerOpen API SDK

***安装***


git clone https://github.com/yourusername/quantitative-trading-system.git

cd quantitative-trading-system

***设置TigerOpen API***


从老虎量化获取API凭证和私钥。

将您的私钥放置在适当的目录（例如，/home/admin/mykey.pem）
