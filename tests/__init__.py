# -*- coding: utf-8 -*-
"""测试包

两个环境变量在任何测试模块导入 rate / ai 之前设好：
  HTS_RECALL_CHANNELS=keyword  归类召回只用离线关键词通道。语义与先例通道依赖本机 ollama
                               与两套向量索引，测试结果不能随"ollama 在不在"而变；
                               三通道本身由 tests/test_recall.py 用假通道覆盖。
  AI_CACHE=0                   关掉 LLM 调用缓存。mock 掉 httpx 的测试要看到每一次请求，
                               缓存命中会让 mock 收不到调用；缓存本身由 test_recall 单测。
"""
import os

os.environ.setdefault("HTS_RECALL_CHANNELS", "keyword")
os.environ.setdefault("AI_CACHE", "0")
