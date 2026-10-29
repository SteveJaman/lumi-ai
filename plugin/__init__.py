"""Lumi's plugins and game agents.

A real package so `from plugin import game_vision` resolves here even when
a script in this folder runs directly: Python then puts this folder on the
path, where plugin/plugin.py would otherwise shadow the package name.
"""
