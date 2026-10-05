"""Stub tối thiểu của pydantic-settings CHỈ cho test: app/config.py của Subtitle_supperVip chỉ cần BaseSettings/SettingsConfigDict."""


class BaseSettings:
    def __init__(self, **kw):
        pass


def SettingsConfigDict(**kw):
    return dict(kw)
