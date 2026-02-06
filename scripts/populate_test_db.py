import sqlite3
import pandas as pd
from pathlib import Path

DIR = Path(__file__).parent.parent

conn = sqlite3.connect(DIR / "db.sqlite3")

players = pd.read_csv(DIR / "testdata" / "players.csv")
players.to_sql("players", conn, if_exists="replace", index=False)

conn.close()
