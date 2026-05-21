#!/usr/bin/env python3
"""Export trades.db to CSV for spreadsheet or further analysis."""
import sqlite3
import sys
from pathlib import Path

try:
    import pandas as pd
except ImportError:
    print("pandas required: pip install pandas")
    sys.exit(1)

DB_PATH = Path(__file__).parent.parent / "data" / "trades.db"


def export(db_path: Path, out_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    df = pd.read_sql_query("SELECT * FROM trades ORDER BY created_at ASC", conn)
    conn.close()
    df.to_csv(out_path, index=False)
    print(f"Exported {len(df)} trades to {out_path}")


def main():
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DB_PATH
    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("trades_export.csv")
    if not db_path.exists():
        print(f"Database not found: {db_path}")
        sys.exit(1)
    export(db_path, out_path)


if __name__ == "__main__":
    main()
