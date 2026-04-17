import sqlite3
from dataclasses import dataclass

try:
    from .db import _cache, ALL_KEYS
except ImportError:
    from db import _cache, ALL_KEYS


@dataclass
class ContactItem:
    username: str
    alias: str
    remark: str
    nick_name: str
    big_head_url: str

    @property
    def dict(self) -> dict:
        return self.__dict__

    @property
    def format_name(self) -> str:
        if self.remark:
            return self.remark
        return self.nick_name


class ContactDB:

    def __init__(self):
        self.__db_connection = None

    @property
    def db_connection(self):
        if self.__db_connection is None:
            self.__db_connection = sqlite3.connect(_cache.get("contact\contact.db"))
        return self.__db_connection

    def get_contact_by_wxid(self, wxid: str) -> ContactItem:
        result = self.db_connection.execute("SELECT username, alias, remark, nick_name, big_head_url from contact where username = ?", (wxid,)).fetchone()
        return ContactItem(*result)

    def get_all_contact(self) -> list[ContactItem]:
        result = self.db_connection.execute("SELECT username, alias, remark, nick_name, big_head_url from contact").fetchall()
        return [ContactItem(*item) for item in result]

    def get_contact_by_keywords(self, keywords: str) -> list[ContactItem]:
        result = self.db_connection.execute("SELECT username, alias, remark, nick_name, big_head_url from contact where username like ? or alias like ? or remark like ? or nick_name like ?", (f"%{keywords}%", f"%{keywords}%", f"%{keywords}%", f"%{keywords}%")).fetchall()
        return [ContactItem(*item) for item in result]

    def close(self):
        if self.__db_connection is not None:
            self.__db_connection.close()

