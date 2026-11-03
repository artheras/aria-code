"""Aria's chat bots: Feishu and Telegram adapters, the relay client, and the
logistics tools they answer shipper questions with.

This package runs as its own service (services/aria-bots). It does not import
aria_code; aria_code keeps shims at the old module paths until the Python core
is removed.
"""
