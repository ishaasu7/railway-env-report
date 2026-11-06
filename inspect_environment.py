#!/usr/bin/env python3
"""One-shot read-only inventory using the same safe collector as the web service.

Importing web_service gathers metadata but does not bind a port or start threads.
No environment dump, package install, external host scan, or service is started.
"""
import json
from web_service import STATE


def main():
    report, _refreshed, _error = STATE.snapshot()
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
