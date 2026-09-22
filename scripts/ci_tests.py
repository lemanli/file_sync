"""让CI失败信息直接出现在检查注释中，便于不下载日志也能定位。"""
import sys
import unittest

patterns = sys.argv[1:] or ['test_*.py']
suite = unittest.TestSuite()
for pattern in patterns:
    suite.addTests(unittest.defaultTestLoader.discover('test', pattern=pattern))
result = unittest.TextTestRunner(verbosity=2).run(suite)
for test, detail in result.failures + result.errors:
    message = (str(test)+'\n'+detail).replace('%','%25').replace('\r','%0D').replace('\n','%0A')
    print('::error::'+message)
raise SystemExit(not result.wasSuccessful())
