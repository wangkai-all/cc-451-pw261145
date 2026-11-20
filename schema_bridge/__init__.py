"""Schema Bridge —— 事件模式转换服务。

支持 v1（扁平结构）与 v2（信封+嵌套结构）两种事件字段结构，
提供双写、回放、消费者版本选择、隔离队列、下线影响报告与幂等保证。
"""

from .converter import ConversionError, InvalidEvent
from .service import SchemaBridgeService

__all__ = ["SchemaBridgeService", "ConversionError", "InvalidEvent"]
__version__ = "1.0.0"
