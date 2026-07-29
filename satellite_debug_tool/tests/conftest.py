"""测试环境兜底：跑测试时永远不发起后台升级检查（避免误打 Gitee API）。"""
import os

os.environ.setdefault("SATELLITE_NO_UPDATE_CHECK", "1")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# Historical UI assertions use the Chinese product copy. M16-specific tests
# explicitly switch to both locales and verify runtime retranslation.
os.environ.setdefault("SATELLITE_DEBUG_LOCALE", "zh_CN")
