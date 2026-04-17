"""
微信消息 DB 查询模块。
提供 MessageDB 类，封装 message_*.db 的连接和查询。
"""

import re
import sqlite3
import hashlib
import zstandard
from dataclasses import dataclass, field
from typing import List, Optional

import xmltodict
from enum import IntEnum, Enum
from dateutil import parser as dateutil_parser

try:
    from .db import _cache, ALL_KEYS
    from .contact import ContactDB
except ImportError:
    from db import _cache, ALL_KEYS
    from contact import ContactDB


class MessageType(IntEnum):
    """
    定义各种消息类型的常量，对应数据库 local_type 字段。
    """
    TEXT = 1    #: 文本消息
    IMAGE = 3   #: 图片消息
    VOICE = 34  #: 语音消息
    FRIEND_VERIFY = 37  #: 好友验证请求消息
    CARD = 42   #: 名片/卡片消息
    VIDEO = 43  #: 视频消息
    EMOJI = 47  #: 表情消息
    LOCATION = 48   #: 位置消息
    XML = 49    #: XML消息（公众号文章、小程序、文件等）
    VOIP = 50   #: 视频/语音通话消息
    PHONE = 51  #: 手机端同步消息
    NOTICE = 10000  #: 通知消息（系统通知、群公告等）
    SYSTEM = 10002  #: 系统消息
    UNKNOWN = -1

class XMLMessageType(Enum):
    REFER = "57"    # 引用消息
    PAT = "62"    # 拍一拍
    MINI_PROGRAM_OLD = "36"   # 也是小程序？有个美团的小程序命中这个类型了
    MINI_PROGRAM = "33" # 小程序
    RED_PACKET = "2001"  #: 红包消息
    VIDEO_CHANNEL = "51"    # 视频号消息
    WEB_LINK = "5"    # 链接
    VIDEO_LINK = "4"    # 视频类链接
    CHAT_RECORD = "19"  # 聊天记录合集
    AUDIO_LINK = "3"    # 同样的音频消息，测试是来着网易云可直接播放的卡片
    AUDIO_MESSAGE = "92"    # 音频类消息，测试是来自QQ音乐的可直接播放的卡片
    GROUP_NOTICE = "87"  # 群公告的@全体成员消息
    FILE = "6"  # 文件


@dataclass
class MessageItem:
    """
    单条消息
    """
    local_id: int
    server_id: int
    local_type: int
    sort_seq: int
    real_sender_id: int
    create_time: int
    status: int
    upload_status: int
    download_status: int
    server_seq: int
    origin_source: int
    source: str
    message_content: str
    compress_content: str
    packed_info_data: bytes
    WCDB_CT_message_content: int
    WCDB_CT_source: int
    sender_wxid: str
    select_wxid: str

    @property
    def __is_xml_message(self) -> bool:
        """判断消息内容是否为 XML 格式"""
        if not isinstance(self.message_content, str):
            return False
        content = self.message_content.strip()
        # 检查是否以 XML 声明或标签开头
        xml_patterns = [
            r'^<\?xml\s+version=',  # <?xml version="1.0">
            r'^<[a-zA-Z_][\w\-\.]*>',  # <tag>
            r'^<[a-zA-Z_][\w\-\.]*\s+',  # <tag attribute=
            r'^[^:]+:\s+<[a-zA-Z_]',  # wxid_xxx: <tag>
        ]
        for pattern in xml_patterns:
            if re.search(pattern, content, re.IGNORECASE):
                return True
        # 检查是否包含完整的 XML 结构（可选，更严格）
        if content.count('<') > 0 and content.count('>') > 0:
            # 简单检查标签是否成对（不一定需要）
            if content.count('</') > 0 or content.endswith('/>'):
                return True
        return False

    @property
    def __clean_message_wxid(self) -> List[str]:
        if not self.chatroom:
            return [self.message_content]
        return self.message_content.split(':\n', 1)

    @property
    def original_message(self):
        if not self.__is_xml_message:
            return self.__clean_message_wxid[-1]
        start_with_wxid_pattern = re.compile(r'^[^:]+:\s+<\?*[a-zA-Z_]')
        xml = self.message_content
        if start_with_wxid_pattern.search(xml):
            _, xml = self.__clean_message_wxid
        if not xml.startswith('<'):
            return xml
        try:
            return xmltodict.parse(xml)
        except Exception as e:
            return self.message_content

    @property
    def format_message(self):
        return MessageProcessor().process(self)


    @property
    def original_source(self):
        try:
            return xmltodict.parse(self.source)
        except Exception:
            return {}

    def __post_init__(self):
        _zstd = zstandard.ZstdDecompressor()
        if self.WCDB_CT_message_content == 4 and isinstance(self.message_content, bytes):
            self.message_content = _zstd.decompress(self.message_content).decode('utf-8', errors='replace')
        if self.WCDB_CT_source == 4 and isinstance(self.source, bytes):
            self.source = _zstd.decompress(self.source).decode('utf-8', errors='replace')

    @property
    def message_type(self) -> MessageType:
        try:
            return MessageType(self.local_type)
        except ValueError:
            return MessageType.UNKNOWN

    # 提及的用户
    @property
    def at_user_list(self) -> List[str]:
        if not self.original_source:
            return []
        msgsource = self.original_source.get('msgsource', {})
        if not msgsource:
            return []
        str_list = msgsource.get('atuserlist', "")
        if not str_list:
            return []
        return str_list.split(',')

    @property
    def chatroom(self) -> Optional[str]:
        """消息是否来自群聊，如果是群聊直接返回群聊wxid，否则None"""
        if "@chatroom" in self.select_wxid:
            return self.select_wxid
        return None


class MessageProcessor:
    """消息处理工厂，根据消息类型路由到不同处理器，归一化返回 dict"""

    def process(self, item: MessageItem) -> dict:
        default_method = self._process_xml
        methods = {
            MessageType.TEXT: self._process_text,
            MessageType.IMAGE: self._process_image,
            MessageType.VOICE: self._process_voice,
            MessageType.VIDEO: self._process_video,
            MessageType.EMOJI: self._process_emoji,
            MessageType.LOCATION: self._process_location,
            MessageType.CARD: self._process_card,
            MessageType.NOTICE: self._process_notice,
            MessageType.SYSTEM: self._process_system,
            MessageType.VOIP: self._process_voip,
        }

        method = methods.get(item.message_type, default_method)
        result = method(item)
        base_info = {
            "sender": item.sender_wxid,
            "server_id": item.server_id,
            "timestamp": item.create_time,
            "select_wxid": item.select_wxid,
            "at_user_list": item.at_user_list,
            "local_id": item.local_id
        }

        return {**base_info, **result}

    def _process_voip(self, item: MessageItem):
        voip_msg = item.original_message.get('voipmsg', {})
        if not voip_msg:
            content = "[通话消息]"
        else:
            content = f"[通话消息] {voip_msg.get('VoIPBubbleMsg', {}).get('msg')}"

        return {
            "type": "通话消息",
            "content": content,
        }

    def _process_text(self, item: MessageItem) -> dict:
        return {
            "type": "文本消息",
            "content": item.original_message,
        }

    def _process_image(self, item: MessageItem) -> dict:
        return {
            "type": "图片消息",
            "content": "[图片]",
        }

    def _process_voice(self, item: MessageItem) -> dict:
        return {
            "type": "语音消息",
            "content": "[语音]",
        }

    def _process_video(self, item: MessageItem) -> dict:
        return {
            "type": "视频消息",
            "content": "[视频]",
        }

    def _process_emoji(self, item: MessageItem) -> dict:
        return {
            "type": "表情消息",
            "content": "[表情]",
        }

    def _process_location(self, item: MessageItem) -> dict:
        xml_dict = item.original_message
        if isinstance(xml_dict, dict):
            appmsg = xml_dict.get("msg", {}).get('location', {})
            label = appmsg.get("@label", "")
            poiname = appmsg.get("@poiname", "")
            content = f"[位置] {label} {poiname}".strip()
        else:
            content = "[位置]"
        return {
            "type": "位置消息",
            "content": content,
        }

    def _process_card(self, item: MessageItem) -> dict:
        xml_dict = item.original_message
        nickname = ""
        username = ""
        if isinstance(xml_dict, dict):
            appmsg = xml_dict.get("msg", {})
            nickname = appmsg.get("@nickname", "")
            username = appmsg.get("@username", "")
        content = f"[名片] {nickname}" if nickname else "[名片]"
        return {
            "type": "名片消息",
            "content": content,
            "crad_wxid": username,
        }

    def _process_notice(self, item: MessageItem) -> dict:
        content = item.message_content
        if isinstance(item.original_message, dict):
            sysmsg = item.original_message.get("sysmsg", {})
            if sysmsg.get("@type") == "sysmsgtemplate":
                template_data = sysmsg.get("sysmsgtemplate", {}).get("content_template", {})
                template = template_data.get("template", "")
                if template:
                    content = self._resolve_sysmsg_template(template, template_data)
        return {
            "type": "系统通知",
            "content": content,
        }

    def _resolve_sysmsg_template(self, template: str, template_data: dict) -> str:
        link_list = template_data.get("link_list", {}).get("link", [])
        if isinstance(link_list, dict):
            link_list = [link_list]
        link_map = {}
        for link in link_list:
            name = link.get("@name", "")
            link_type = link.get("@type", "")
            if link_type == "link_profile":
                members = link.get("memberlist", {}).get("member", [])
                if isinstance(members, dict):
                    members = [members]
                names = [m.get("nickname", m.get("username", "")) for m in members]
                separator = link.get("separator", "、")
                link_map[f"${name}$"] = separator.join(names)
            elif link_type == "link_plain":
                link_map[f"${name}$"] = link.get("plain", "")
        for key, value in link_map.items():
            template = template.replace(key, value)
        return template

    def _process_system(self, item: MessageItem) -> dict:
        return {
            "type": "系统消息",
            "content": item.message_content,
        }

    def _process_xml(self, item: MessageItem) -> dict:
        xml_dict = item.original_message
        if not isinstance(xml_dict, dict):
            return {
                "type": "未知消息",
                "content": str(xml_dict),
            }

        appmsg = xml_dict.get("msg", {}).get("appmsg", {})
        xml_type = appmsg.get("type", "?")
        appinfo = xml_dict.get("msg", {}).get("appinfo", {})

        handlers = {
            "33": lambda: self._process_mini_program_33(appmsg),
            "36": lambda: self._process_mini_program_36(appmsg, appinfo),
            "5": lambda: self._process_link(appmsg, appinfo),
            "4": lambda: self._process_video_link(appmsg, appinfo),
            "19": lambda: self._process_chat_record(appmsg),
            "57": lambda: self._process_refer(appmsg),
            "62": lambda: self._process_pat(appmsg),
            "2001": lambda: self._process_red_packet(appmsg),
            "51": lambda: self._process_video_channel(appmsg),
            "87": lambda: self._process_group_notice(appmsg),
            "6": lambda: self._process_file(appmsg),
            "3": lambda: self._process_audio(appmsg, appinfo),
            "92": lambda: self._process_audio(appmsg, appinfo),
        }

        handler = handlers.get(xml_type)
        if handler:
            result = handler()
        else:
            result = {
                "type": "XML消息",
                "content": appmsg.get("title", appmsg.get("des", "")),
            }

        return result

    def _process_mini_program_33(self, appmsg: dict) -> dict:
        sourcedisplayname = appmsg.get("sourcedisplayname", "")
        title = appmsg.get("title", "")
        return {
            "type": "小程序",
            "content": f"[小程序: {sourcedisplayname}] {title}" if sourcedisplayname else f"[小程序] {title}",
        }

    def _process_mini_program_36(self, appmsg: dict, appinfo: dict) -> dict:
        appname = appinfo.get("appname", "")
        title = appmsg.get("title", "")
        return {
            "type": "小程序",
            "content": f"[小程序: {appname}] {title}" if appname else f"[小程序] {title}",
        }

    def _process_link(self, appmsg: dict, appinfo: dict) -> dict:
        sourcedisplayname = appmsg.get("sourcedisplayname", "")
        appname = appinfo.get("appname", "")
        source = sourcedisplayname or appname
        title = appmsg.get("title", "")
        if source:
            content = f"[链接: {source}] {title}"
        else:
            content = f"[链接] {title}"
        return {
            "type": "链接",
            "content": content,
        }

    def _process_video_link(self, appmsg: dict, appinfo: dict) -> dict:
        appname = appinfo.get("appname", "")
        title = appmsg.get("title", "")
        if appname:
            content = f"[视频链接: {appname}] {title}"
        else:
            content = f"[视频链接] {title}"
        return {
            "type": "视频链接",
            "content": content,
        }

    def _process_chat_record(self, appmsg: dict) -> dict:
        title = appmsg.get("title", "")
        des = appmsg.get("des", "")
        return {
            "type": "聊天记录",
            "content": f"[聊天记录] {title}",
            "preview": des,
        }

    def _process_refer(self, appmsg: dict) -> dict:
        title = appmsg.get("title", "")
        refermsg = appmsg.get("refermsg", {})
        return {
            "type": "引用消息",
            "content": f"{title}",
            "refer_svrid": refermsg.get("svrid", ""),
            "refer_fromusr": refermsg.get("fromusr", ""),
            "refer_displayname": refermsg.get("displayname", ""),
        }

    def _process_pat(self, appmsg: dict) -> dict:
        patinfo = appmsg.get("patinfo", {})
        template = patinfo.get("template", "")
        pat_from_username = patinfo.get("fromusername", "")
        pat_ted_username = patinfo.get("pattedusername", "")
        if pat_from_username == pat_ted_username:
            content = f"[拍一拍] {template}"
        else:
            pat_suffix = patinfo.get("patsuffix", "")
            contact = ContactDB()
            pat_from_username_item = contact.get_contact_by_wxid(pat_from_username)
            pat_ted_username_item = contact.get_contact_by_wxid(pat_ted_username)
            content = f"[拍一拍] {pat_from_username_item.format_name} 拍了拍 {pat_ted_username_item.format_name} {pat_suffix}"


        return {
            "type": "拍一拍",
            "content": content,
            "pat_from_username": pat_from_username,
            "pat_ted_username": pat_ted_username,
        }

    def _process_red_packet(self, appmsg: dict) -> dict:
        des = appmsg.get("des", "")
        return {
            "type": "红包",
            "content": f"[微信红包] {des}" if des else "[微信红包]",
        }

    def _process_video_channel(self, appmsg: dict) -> dict:
        finder = appmsg.get("finderFeed", {})
        nickname = finder.get("nickname", "")
        desc = finder.get("desc", "")
        if nickname:
            content = f"[视频号: {nickname}] {desc}"
        else:
            content = f"[视频号] {desc}"
        return {
            "type": "视频号",
            "content": content,
        }

    def _process_group_notice(self, appmsg: dict) -> dict:
        textannouncement = appmsg.get("textannouncement", "")
        return {
            "type": "群公告",
            "content": f"[群公告] @全体成员 {textannouncement}" if textannouncement else "[群公告]",
        }

    def _process_file(self, appmsg: dict) -> dict:
        title = appmsg.get("title", "")
        return {
            "type": "文件",
            "content": f"[文件] {title}" if title else "[文件]",
        }

    def _process_audio(self, appmsg: dict, appinfo: dict) -> dict:
        appname = appinfo.get("appname", "")
        title = appmsg.get("title", "")
        des = appmsg.get("des", "")
        if appname:
            content = f"[音频: {appname}] {title} - {des}" if des else f"[音频: {appname}] {title}"
        else:
            content = f"[音频] {title} - {des}" if des else f"[音频] {title}"
        return {
            "type": "音频",
            "content": content,
        }


@dataclass
class DBConnectionItem:
    key: str
    connect: sqlite3.Connection

    @property
    def all_msg_table(self) -> List[str]:
        _result = self.run("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%' ORDER BY name DESC;").fetchall()
        return [item[0] for item in _result]

    def get_table_by_wxid(self, wx_id: str) -> List[str]:
        hash_str = hashlib.md5(wx_id.encode()).hexdigest()
        _result = self.run("SELECT name FROM sqlite_master WHERE type='table' AND name = ?;", f"Msg_{hash_str}").fetchall()
        return [item[0] for item in _result]

    def get_messages(self, wxid: str, start_time: str, end_time: str, limit: int = None) -> List[MessageItem]:
        table_list = self.get_table_by_wxid(wxid)
        if not table_list:
            return []
        start_time_stamp = int(dateutil_parser.parse(start_time).timestamp())
        end_time_stamp = int(dateutil_parser.parse(end_time).timestamp())
        if end_time_stamp < start_time_stamp:
            start_time_stamp, end_time_stamp = end_time_stamp, start_time_stamp
        limit = f"LIMIT {limit}" if isinstance(limit, int) else ""

        table = table_list[0]
        sql = f"SELECT m.*, n.user_name as sender_wxid " \
              f"FROM {table} m " \
              f"LEFT JOIN Name2Id n ON m.real_sender_id = n.rowid " \
              f"WHERE m.create_time >= {start_time_stamp} " \
              f"AND m.create_time <= {end_time_stamp} " \
              f"ORDER BY m.create_time ASC {limit};"

        return [MessageItem(*[*_item, wxid]) for _item in self.run(sql).fetchall()]

    def run(self, sql: str, *args) -> sqlite3.Cursor:
        return self.connect.execute(sql, args)

    def close(self):
        self.connect.close()



class MessageDB:
    MESSAGE_DB_KEY_PATTERN = re.compile(r"^message[\\/]message_\d+\.db$")

    def __init__(self):
        self.contact = ContactDB()
        self.__all_message_db_connection: Optional[List[DBConnectionItem]] = None

    @property
    def __all_db_file(self) -> List[str]:
        return [item for item in ALL_KEYS.keys() if re.search(self.MESSAGE_DB_KEY_PATTERN, item)]

    @property
    def all_db_connection(self) -> List[DBConnectionItem]:
        if self.__all_message_db_connection is None:
            self.__all_message_db_connection = [
                DBConnectionItem(item, sqlite3.connect(_cache.get(item)))
                for item in self.__all_db_file
            ]
        return self.__all_message_db_connection

    def close_all(self):
        if self.__all_message_db_connection is not None:
            for conn_item in self.__all_message_db_connection:
                conn_item.close()
            self.__all_message_db_connection = None
        self.contact.close()


    def get_messages(self, wxid: str, start_time: str, end_time: str, limit: int = None) -> List[MessageItem]:
        result: List[MessageItem] = []
        for db_item in self.all_db_connection:
            result += db_item.get_messages(wxid, start_time, end_time, limit)
        result.sort(key=lambda item: item.create_time)
        return result

    def get_message_by_server_id(self, server_id: str, wxid: str) -> Optional[MessageItem]:
        for db_item in self.all_db_connection:
            table_name = db_item.get_table_by_wxid(wxid)
            if not table_name:
                continue
            table_name = table_name[0]
            sql = f"SELECT m.*, n.user_name as sender_wxid " \
                  f"FROM {table_name} m " \
                  f"LEFT JOIN Name2Id n ON m.real_sender_id = n.rowid " \
                  f"WHERE m.server_id = ?;"
            result = db_item.run(sql, server_id).fetchone()
            if result:
                return MessageItem(*[*result, wxid])
        return None