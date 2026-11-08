"""Normalize configured host bind prefixes for display without changing I/O paths."""

import re


def display_host_path(value, mappings):
    if not isinstance(value, str):
        return value
    for prefix, destination in sorted(mappings.items(), key=lambda item: len(item[0]), reverse=True):
        prefix = prefix.rstrip('/')
        if not prefix:
            continue
        pattern = r'(?<![\w/.-])' + re.escape(prefix) + r'(?=/|$|[\s:\)\]\}\"\'])'
        def replace(match):
            suffix = value[match.end():]
            return destination.rstrip('/') if suffix.startswith('/') else destination
        value = re.sub(pattern, replace, value)
    return value
