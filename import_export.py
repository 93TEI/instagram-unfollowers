"""Optional offline import of an already-downloaded official export.

Never imports instagram.py, reads a session, or connects to any service.
"""

import argparse
import json

from export_data import ExportStore
from export_jobs import account_lock


def main():
    parser = argparse.ArgumentParser(description="공식 인스타그램 JSON ZIP/폴더를 로컬로 가져옵니다.")
    parser.add_argument("--account-id", required=True, help="내보내기 소유자의 인스타그램 숫자 계정 ID")
    parser.add_argument("--username", required=True)
    parser.add_argument("sources", nargs="+", help="한 번의 내보내기에 속한 ZIP 파일들 또는 폴더")
    args = parser.parse_args()
    store = ExportStore(args.account_id)
    with account_lock(store) as locked:
        if not locked:
            parser.error("다른 내보내기 작업이 진행 중입니다.")
        result = store.import_sources(args.sources, args.username)
        # An import does not cancel or resubmit any existing remote export job.
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
