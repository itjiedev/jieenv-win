"""
MySQL 管理操作封装（基于 mysqlclient/MySQLdb）。

提供登录测试、库管理、表管理、字段管理所需的全部 SQL 操作。
所有标识符（库名/表名/字段名）经过严格白名单校验后再反引号包裹，
值类参数一律走 cursor 参数绑定，防止 SQL 注入。
"""
import re
import logging

import MySQLdb

logger = logging.getLogger(__name__)

# 标识符白名单：字母/数字/下划线/$ 及中文，最长 64
_IDENT_RE = re.compile(r'^[0-9A-Za-z_$\u4e00-\u9fff]{1,64}$')
# 字符集/排序规则/存储引擎：仅小写字母数字下划线
_CHARSET_RE = re.compile(r'^[a-z0-9_]{1,32}$')

CONNECT_TIMEOUT = 8

# ── 字段类型注册表（前端下拉列表与后端白名单校验共用） ──
# arg: ''=无参数 / 'length'=长度 / 'precision'=精度+小数位 / 'values'=枚举取值
FIELD_TYPES = [
    {'name': 'TINYINT', 'group': '整数', 'arg': 'length', 'unsigned': True, 'tip': '微整数（1字节）'},
    {'name': 'SMALLINT', 'group': '整数', 'arg': 'length', 'unsigned': True, 'tip': '小整数（2字节）'},
    {'name': 'MEDIUMINT', 'group': '整数', 'arg': 'length', 'unsigned': True, 'tip': '中整数（3字节）'},
    {'name': 'INT', 'group': '整数', 'arg': 'length', 'unsigned': True, 'tip': '普通整数（4字节）'},
    {'name': 'BIGINT', 'group': '整数', 'arg': 'length', 'unsigned': True, 'tip': '大整数（8字节）'},
    {'name': 'BIT', 'group': '整数', 'arg': 'length', 'unsigned': False, 'tip': '位字段，长度 1~64'},
    {'name': 'DECIMAL', 'group': '小数', 'arg': 'precision', 'unsigned': True, 'tip': '精确小数，金额类字段推荐使用'},
    {'name': 'FLOAT', 'group': '小数', 'arg': 'precision', 'unsigned': True, 'tip': '单精度浮点'},
    {'name': 'DOUBLE', 'group': '小数', 'arg': 'precision', 'unsigned': True, 'tip': '双精度浮点'},
    {'name': 'CHAR', 'group': '字符串', 'arg': 'length', 'unsigned': False, 'tip': '定长字符串，不填长度默认 1'},
    {'name': 'VARCHAR', 'group': '字符串', 'arg': 'length', 'arg_required': True, 'unsigned': False, 'tip': '变长字符串，长度必填（最大 65535）'},
    {'name': 'TINYTEXT', 'group': '字符串', 'arg': '', 'unsigned': False, 'tip': '微文本（255 字节）'},
    {'name': 'TEXT', 'group': '字符串', 'arg': '', 'unsigned': False, 'tip': '文本（64 KB）'},
    {'name': 'MEDIUMTEXT', 'group': '字符串', 'arg': '', 'unsigned': False, 'tip': '中文本（16 MB）'},
    {'name': 'LONGTEXT', 'group': '字符串', 'arg': '', 'unsigned': False, 'tip': '长文本（4 GB）'},
    {'name': 'ENUM', 'group': '字符串', 'arg': 'values', 'unsigned': False, 'tip': '单选枚举，取值用逗号分隔'},
    {'name': 'SET', 'group': '字符串', 'arg': 'values', 'unsigned': False, 'tip': '多选集合，取值用逗号分隔'},
    {'name': 'BINARY', 'group': '二进制', 'arg': 'length', 'unsigned': False, 'tip': '定长二进制'},
    {'name': 'VARBINARY', 'group': '二进制', 'arg': 'length', 'arg_required': True, 'unsigned': False, 'tip': '变长二进制，长度必填'},
    {'name': 'TINYBLOB', 'group': '二进制', 'arg': '', 'unsigned': False, 'tip': '微二进制（255 字节）'},
    {'name': 'BLOB', 'group': '二进制', 'arg': '', 'unsigned': False, 'tip': '二进制（64 KB）'},
    {'name': 'MEDIUMBLOB', 'group': '二进制', 'arg': '', 'unsigned': False, 'tip': '中二进制（16 MB）'},
    {'name': 'LONGBLOB', 'group': '二进制', 'arg': '', 'unsigned': False, 'tip': '长二进制（4 GB）'},
    {'name': 'DATE', 'group': '日期时间', 'arg': '', 'unsigned': False, 'tip': '日期 YYYY-MM-DD'},
    {'name': 'TIME', 'group': '日期时间', 'arg': '', 'unsigned': False, 'tip': '时间 HH:MM:SS'},
    {'name': 'DATETIME', 'group': '日期时间', 'arg': '', 'unsigned': False, 'tip': '日期时间 YYYY-MM-DD HH:MM:SS'},
    {'name': 'TIMESTAMP', 'group': '日期时间', 'arg': '', 'unsigned': False, 'tip': '时间戳'},
    {'name': 'YEAR', 'group': '日期时间', 'arg': '', 'unsigned': False, 'tip': '年份'},
    {'name': 'JSON', 'group': '其他', 'arg': '', 'unsigned': False, 'tip': 'JSON 文档（MySQL 5.7+）'},
]

_ALLOWED_TYPE_NAMES = {t['name'] for t in FIELD_TYPES} | {'INTEGER', 'NUMERIC', 'BOOLEAN', 'BOOL'}
_UNSIGNED_ALLOWED = {'TINYINT', 'SMALLINT', 'MEDIUMINT', 'INT', 'INTEGER',
                     'BIGINT', 'DECIMAL', 'NUMERIC', 'FLOAT', 'DOUBLE'}
_LENGTH_REQUIRED = {'VARCHAR', 'VARBINARY'}

SYS_DB = ['sys', 'information_schema', 'mysql', 'performance_schema']

class MysqlError(Exception):
    """面向用户的 MySQL 操作错误。

    可选属性:
      errno: 原始 MySQL errno（如 1045 / 2002 / 2003 / 2006），供上层分类自愈。
    """
    def __init__(self, message: str = '', errno: int = 0):
        super().__init__(message)
        self.errno = errno


def quote_ident(name: str) -> str:
    """校验并反引号包裹标识符"""
    if not name or not _IDENT_RE.match(name):
        raise MysqlError(f'非法的标识符: {name!r}')
    return '`' + name.replace('`', '``') + '`'


def check_charset(charset: str, collation: str = '') -> tuple:
    """校验字符集与排序规则，返回 (charset, collation)"""
    charset = (charset or '').strip().lower()
    collation = (collation or '').strip().lower()
    if charset and not _CHARSET_RE.match(charset):
        raise MysqlError(f'非法的字符集: {charset!r}')
    if collation and not _CHARSET_RE.match(collation):
        raise MysqlError(f'非法的排序规则: {collation!r}')
    return charset, collation


def connect(host: str, port, user: str, password: str, db: str = '',
            timeout: int = CONNECT_TIMEOUT):
    """建立连接，失败抛 MysqlError（带可读消息）"""
    try:
        kwargs = dict(host=host, port=int(port), user=user, passwd=password,
                      charset='utf8mb4', connect_timeout=timeout)
        if db:
            kwargs['db'] = db
        return MySQLdb.connect(**kwargs)
    except MySQLdb.OperationalError as e:
        code = e.args[0] if e.args else 0
        if code == 1045:
            raise MysqlError('用户名或密码错误', errno=code) from e
        if code == 2003:
            raise MysqlError(f'无法连接到 {host}:{port}，请确认 MySQL 已启动且端口正确', errno=code) from e
        if code == 2002:
            raise MysqlError(f'无法连接到 {host}:{port}，MySQL 服务未启动或端口不可达', errno=code) from e
        raise MysqlError(f'连接失败: {e}', errno=code) from e
    except Exception as e:
        raise MysqlError(f'连接失败: {e}') from e


def test_login(host: str, port, user: str, password: str) -> dict:
    """登录测试，成功返回 {success, version}，失败抛 MysqlError"""
    conn = connect(host, port, user, password)
    try:
        cur = conn.cursor()
        cur.execute('SELECT VERSION()')
        version = cur.fetchone()[0]
        return {'success': True, 'version': version}
    finally:
        conn.close()


def change_password(conn, new_password: str):
    """修改当前连接用户的密码（MySQL 5.7+ / 8.0+）"""
    try:
        cur = conn.cursor()
        cur.execute('ALTER USER USER() IDENTIFIED BY %s', (new_password,))
        conn.commit()
    except MySQLdb.MySQLError as e:
        raise MysqlError(f'修改密码失败: {e}') from e


def _exec(conn, sql: str, params: tuple = ()):
    cur = conn.cursor()
    cur.execute(sql, params)
    return cur


# ── 数据库管理 ───────────────────────────────────────────

def list_databases(conn) -> list:
    """返回 [{name, charset, collation, comment}]"""
    cur = _exec(conn, "SELECT SCHEMA_NAME, DEFAULT_CHARACTER_SET_NAME, "
                      "DEFAULT_COLLATION_NAME FROM information_schema.SCHEMATA "
                      "ORDER BY SCHEMA_NAME")
    rows = cur.fetchall()
    result = []
    for name, charset, collation in rows:
        result.append({'name': name, 'charset': charset, 'collation': collation})
    # 附带表数量
    cur = _exec(conn, "SELECT TABLE_SCHEMA, COUNT(*) FROM information_schema.TABLES "
                      "WHERE TABLE_SCHEMA NOT IN ('information_schema','performance_schema',"
                      "'mysql','sys') GROUP BY TABLE_SCHEMA")
    table_counts = dict(cur.fetchall())
    for item in result:
        item['table_count'] = table_counts.get(item['name'], 0)
    return result


def create_database(conn, name: str, charset: str = 'utf8mb4', collation: str = ''):
    name_q = quote_ident(name)
    charset, collation = check_charset(charset, collation)
    sql = f'CREATE DATABASE {name_q}'
    if charset:
        sql += f' DEFAULT CHARACTER SET {charset}'
    if collation:
        sql += f' DEFAULT COLLATE {collation}'
    try:
        _exec(conn, sql)
        conn.commit()
    except MySQLdb.MySQLError as e:
        raise MysqlError(f'创建数据库失败: {e}') from e


def drop_database(conn, name: str):
    try:
        _exec(conn, f'DROP DATABASE {quote_ident(name)}')
        conn.commit()
    except MySQLdb.MySQLError as e:
        raise MysqlError(f'删除数据库失败: {e}') from e


# ── 表管理 ───────────────────────────────────────────────

def list_tables(conn, db: str) -> list:
    """返回 [{name, engine, rows, data_length, comment, create_time}]"""
    quote_ident(db)  # 仅校验
    cur = _exec(conn, 'SHOW TABLE STATUS FROM ' + quote_ident(db))
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    result = []
    for row in rows:
        item = dict(zip(cols, row))
        result.append({
            'name': item.get('Name', ''),
            'engine': item.get('Engine') or '',
            'rows': item.get('Rows') or 0,
            'data_length': item.get('Data_length') or 0,
            'comment': item.get('Comment') or '',
            'create_time': str(item.get('Create_time') or ''),
        })
    return result


def create_table(conn, db: str, table: str, engine: str = 'InnoDB',
                 charset: str = 'utf8mb4', collation: str = '',
                 comment: str = ''):
    """
    建表：默认创建自增主键 id 字段，用户可随后到字段管理中添加其他字段。
    """
    db_q, table_q = quote_ident(db), quote_ident(table)
    if not engine or not re.match(r'^[A-Za-z]{1,32}$', engine):
        raise MysqlError(f'非法的存储引擎: {engine!r}')
    charset, collation = check_charset(charset, collation)
    sql = (f'CREATE TABLE {db_q}.{table_q} '
           f'(`id` INT NOT NULL AUTO_INCREMENT PRIMARY KEY) '
           f'ENGINE={engine}')
    if charset:
        sql += f' DEFAULT CHARSET={charset}'
    if collation:
        sql += f' DEFAULT COLLATE={collation}'
    if comment:
        cur = conn.cursor()
        sql += ' COMMENT=%s'
        try:
            cur.execute(sql, (comment,))
        except MySQLdb.MySQLError as e:
            raise MysqlError(f'创建表失败: {e}') from e
    else:
        try:
            _exec(conn, sql)
        except MySQLdb.MySQLError as e:
            raise MysqlError(f'创建表失败: {e}') from e
    conn.commit()


def drop_table(conn, db: str, table: str):
    try:
        _exec(conn, f'DROP TABLE {quote_ident(db)}.{quote_ident(table)}')
        conn.commit()
    except MySQLdb.MySQLError as e:
        raise MysqlError(f'删除表失败: {e}') from e


def truncate_table(conn, db: str, table: str):
    try:
        _exec(conn, f'TRUNCATE TABLE {quote_ident(db)}.{quote_ident(table)}')
        conn.commit()
    except MySQLdb.MySQLError as e:
        raise MysqlError(f'清空表失败: {e}') from e


# ── 字段管理 ─────────────────────────────────────────────

def list_columns(conn, db: str, table: str) -> list:
    """返回字段信息列表（按序号排序）"""
    db_q, table_q = quote_ident(db), quote_ident(table)
    cur = _exec(conn, 'SHOW FULL COLUMNS FROM ' + db_q + '.' + table_q)
    rows = cur.fetchall()
    result = []
    for row in rows:
        # Field, Type, Collation, Null, Key, Default, Extra, Privileges, Comment
        result.append({
            'name': row[0],
            'type': row[1],
            'collation': row[2] or '',
            'nullable': row[3] == 'YES',
            'key': row[4] or '',
            'default': row[5] if row[5] is not None else '',
            'extra': row[6] or '',
            'comment': row[8] or '',
        })
    return result


def _validate_field_type(field_type: str) -> str:
    """
    白名单校验字段类型并归一化，返回可用于 SQL 的类型片段。
    支持: VARCHAR(50)、DECIMAL(10,2)、INT UNSIGNED、ENUM('a','b') 等。
    """
    s = (field_type or '').strip()
    if not s:
        raise MysqlError('字段类型不能为空')
    m = re.match(r'^([A-Za-z]{2,20})\s*(?:\((.*)\))?\s*((?:(?:UNSIGNED|ZEROFILL)\s*)*)$',
                 s, re.IGNORECASE | re.S)
    if not m:
        raise MysqlError(f'非法的字段类型: {field_type!r}')
    name = m.group(1).upper()
    args = m.group(2)
    mods = m.group(3) or ''
    if name not in _ALLOWED_TYPE_NAMES:
        raise MysqlError(f'不支持的字段类型: {name}')

    type_sql = name
    if args is not None:
        args = args.strip()
        if name in ('ENUM', 'SET'):
            if not re.match(r"^'[^']*'(?:\s*,\s*'[^']*')*$", args):
                raise MysqlError(f'{name} 的取值格式非法，应为逗号分隔的带引号值')
            type_sql += f'({re.sub(r"\s*,\s*", ",", args)})'
        else:
            if not re.match(r'^\d+(\s*,\s*\d+)?$', args):
                raise MysqlError(f'{name} 的长度/精度参数非法: {args}')
            type_sql += '(' + re.sub(r'\s+', '', args) + ')'
    elif name in _LENGTH_REQUIRED:
        raise MysqlError(f'{name} 必须指定长度')

    modifiers = []
    for word in mods.split():
        word = word.upper()
        if name not in _UNSIGNED_ALLOWED:
            raise MysqlError(f'{name} 不支持 UNSIGNED/ZEROFILL 修饰')
        if word not in modifiers:
            modifiers.append(word)
    if modifiers:
        type_sql += ' ' + ' '.join(modifiers)
    return type_sql


def parse_field_type(field_type: str) -> dict:
    """
    尽力解析已有字段类型串（如 varchar(50)、int unsigned、enum('a','b')），
    供编辑表单回填。返回 {name, args: [...], unsigned, zerofill}
    """
    result = {'name': '', 'args': [], 'unsigned': False, 'zerofill': False}
    m = re.match(r'^\s*([A-Za-z]{2,20})\s*(?:\((.*)\))?\s*(.*)$',
                 field_type or '', re.IGNORECASE | re.S)
    if not m:
        return result
    result['name'] = m.group(1).upper()
    rest = (m.group(3) or '').upper()
    result['unsigned'] = 'UNSIGNED' in rest
    result['zerofill'] = 'ZEROFILL' in rest
    args = m.group(2)
    if args is not None:
        args = args.strip()
        if result['name'] in ('ENUM', 'SET'):
            result['args'] = re.findall(r"'([^']*)'", args)
        else:
            result['args'] = [p.strip() for p in args.split(',') if p.strip()]
    return result


def _build_column_def(conn, field_type: str, nullable: bool, default: str,
                      has_default: bool, comment: str, primary_key: bool) -> tuple:
    """
    组装字段定义片段，返回 (sql片段, params)。
    field_type 形如 VARCHAR(50) / INT / DECIMAL(10,2)，可带 UNSIGNED。
    """
    type_sql = _validate_field_type(field_type)

    def_sql = type_sql
    params = []
    def_sql += ' NULL' if nullable else ' NOT NULL'
    if has_default:
        if default.strip().upper() == 'CURRENT_TIMESTAMP':
            def_sql += ' DEFAULT CURRENT_TIMESTAMP'
        elif default == '' and nullable:
            def_sql += ' DEFAULT NULL'
        else:
            def_sql += ' DEFAULT %s'
            params.append(default)
    if primary_key:
        def_sql += ' PRIMARY KEY'
    if comment:
        def_sql += ' COMMENT %s'
        params.append(comment)
    return def_sql, tuple(params)


def _position_sql(position: str, after_column: str) -> str:
    if position == 'first':
        return ' FIRST'
    if position == 'after' and after_column:
        return ' AFTER ' + quote_ident(after_column)
    return ''


def add_column(conn, db: str, table: str, name: str, field_type: str,
               nullable: bool, has_default: bool, default: str,
               comment: str, primary_key: bool,
               position: str = '', after_column: str = ''):
    def_sql, params = _build_column_def(conn, field_type, nullable, default,
                                        has_default, comment, primary_key)
    sql = (f'ALTER TABLE {quote_ident(db)}.{quote_ident(table)} '
           f'ADD COLUMN {quote_ident(name)} {def_sql}'
           + _position_sql(position, after_column))
    try:
        _exec(conn, sql, params)
        conn.commit()
    except MySQLdb.MySQLError as e:
        raise MysqlError(f'添加字段失败: {e}') from e


def modify_column(conn, db: str, table: str, name: str, field_type: str,
                  nullable: bool, has_default: bool, default: str,
                  comment: str, position: str = '', after_column: str = ''):
    def_sql, params = _build_column_def(conn, field_type, nullable, default,
                                        has_default, comment, False)
    sql = (f'ALTER TABLE {quote_ident(db)}.{quote_ident(table)} '
           f'MODIFY COLUMN {quote_ident(name)} {def_sql}'
           + _position_sql(position, after_column))
    try:
        _exec(conn, sql, params)
        conn.commit()
    except MySQLdb.MySQLError as e:
        raise MysqlError(f'修改字段失败: {e}') from e


def drop_column(conn, db: str, table: str, name: str):
    sql = (f'ALTER TABLE {quote_ident(db)}.{quote_ident(table)} '
           f'DROP COLUMN {quote_ident(name)}')
    try:
        _exec(conn, sql)
        conn.commit()
    except MySQLdb.MySQLError as e:
        raise MysqlError(f'删除字段失败: {e}') from e


def get_charsets(conn) -> dict:
    """从 MySQL 获取可用字符集及其排序规则。

    返回 {
        'charsets': [字符集名列表],
        'charset_collations': {字符集: {'collations': [...], 'default': '...'}},
        'default_charset': '服务器默认字符集'
    }
    """
    cur = _exec(conn, 'SHOW COLLATION')
    rows = cur.fetchall()
    # 列顺序: Name, Charset, Id, Default, Compiled
    charsets_set = set()
    charset_collations = {}
    for row in rows:
        collation = row[0]
        charset = row[1]
        is_default = row[3] == 'Yes'
        charsets_set.add(charset)
        if charset not in charset_collations:
            charset_collations[charset] = {'collations': [], 'default': ''}
        charset_collations[charset]['collations'].append(collation)
        if is_default:
            charset_collations[charset]['default'] = collation
    # 对每个字符集的排序规则列表排序，默认规则置顶
    for charset, info in charset_collations.items():
        default = info['default']
        info['collations'].sort(key=lambda c: (c != default, c))
    # 获取服务器默认字符集
    default_charset = ''
    try:
        cur = _exec(conn, "SELECT @@character_set_server")
        row = cur.fetchone()
        if row:
            default_charset = row[0]
    except Exception:
        pass
    return {
        'charsets': sorted(charsets_set),
        'charset_collations': charset_collations,
        'default_charset': default_charset,
    }


def get_server_version(conn) -> str:
    cur = _exec(conn, 'SELECT VERSION()')
    return cur.fetchone()[0]


# ── 用户管理 ─────────────────────────────────────────────

# 用户名校验：允许字母/数字/下划线/$，最长 80（MySQL 内部上限）
_USER_RE = re.compile(r'^[0-9A-Za-z_$]{1,80}$')
# HOST 校验：允许 IP / 域名 / localhost / % / 空字符串，最长 255
_HOST_RE = re.compile(r'^[0-9A-Za-z._:%\-]{0,255}$')
# 权限白名单（MySQL 8.0 通用的全局/库级权限）
_ALLOWED_PRIVILEGES = {
    'ALL', 'ALL PRIVILEGES',
    'SELECT', 'INSERT', 'UPDATE', 'DELETE',
    'CREATE', 'DROP', 'INDEX', 'ALTER',
    'REFERENCES', 'CREATE TEMPORARY TABLES', 'LOCK TABLES',
    'CREATE VIEW', 'SHOW VIEW', 'CREATE ROUTINE', 'ALTER ROUTINE',
    'TRIGGER', 'EVENT', 'RELOAD', 'SHUTDOWN', 'PROCESS', 'FILE',
    'CREATE USER', 'GRANT OPTION', 'REPLICATION SLAVE', 'REPLICATION CLIENT',
    'CREATE TABLESPACE',
}


def _validate_user_host(user: str, host: str = ''):
    if not _USER_RE.match(user):
        raise MysqlError(f'非法的用户名: {user!r}')
    if not _HOST_RE.match(host):
        raise MysqlError(f'非法的主机: {host!r}')


def _normalize_privileges(privileges) -> list:
    """将权限参数统一归一化为权限名列表"""
    if isinstance(privileges, str):
        privs = [p.strip().upper() for p in privileges.split(',') if p.strip()]
    elif isinstance(privileges, (list, tuple)):
        privs = [str(p).strip().upper() for p in privileges if str(p).strip()]
    else:
        raise MysqlError('权限参数必须是字符串或列表')
    if not privs:
        raise MysqlError('至少指定一个权限')
    for p in privs:
        if p not in _ALLOWED_PRIVILEGES:
            raise MysqlError(f'不支持的权限: {p!r}')
    return privs


def list_users(conn) -> list:
    """返回 [{user, host, is_active, auth_plugin}]"""
    cur = _exec(conn, "SELECT user, host, account_locked, plugin FROM mysql.user ORDER BY user")
    rows = cur.fetchall()
    return [{'user': r[0], 'host': r[1],
             'is_active': r[2] == 'N',
             'auth_plugin': r[3] or ''}
            for r in rows]


def create_user(conn, user: str, host: str, password: str = ''):
    _validate_user_host(user, host)
    if password:
        sql = f"CREATE USER {quote_ident(user)}@%s IDENTIFIED BY %s"
        params = (host, password)
    else:
        sql = f"CREATE USER {quote_ident(user)}@%s"
        params = (host,)
    try:
        _exec(conn, sql, params)
        conn.commit()
    except MySQLdb.MySQLError as e:
        raise MysqlError(f'创建用户失败: {e}') from e


def drop_user(conn, user: str, host: str = ''):
    _validate_user_host(user, host)
    sql = f"DROP USER {quote_ident(user)}@%s"
    try:
        _exec(conn, sql, (host,))
        conn.commit()
    except MySQLdb.MySQLError as e:
        raise MysqlError(f'删除用户失败: {e}') from e


def grant_privileges(conn, user: str, host: str, privileges,
                     db: str = '*', table: str = '*'):
    """授权。db/table 为 '*' 表示全局授权"""
    _validate_user_host(user, host)
    privs = _normalize_privileges(privileges)
    db_q = quote_ident(db) if db != '*' else '*'
    table_q = quote_ident(table) if table != '*' else '*'
    priv_str = ', '.join(privs)
    sql = f"GRANT {priv_str} ON {db_q}.{table_q} TO {quote_ident(user)}@%s"
    try:
        _exec(conn, sql, (host,))
        _exec(conn, 'FLUSH PRIVILEGES')
        conn.commit()
    except MySQLdb.MySQLError as e:
        raise MysqlError(f'授权失败: {e}') from e


def revoke_privileges(conn, user: str, host: str, privileges,
                      db: str = '*', table: str = '*'):
    """撤销授权。

    privileges 可接受两种格式：
      1) 纯权限名字符串："SELECT, INSERT"（db/table 需同步传入）
      2) SHOW GRANTS 返回的完整 GRANT 语句：自动正则解析出权限、db、table
    """
    _validate_user_host(user, host)
    import re as _re
    grant_stmt = None
    if isinstance(privileges, str) and _re.match(r'^\s*GRANT\s+', privileges, _re.IGNORECASE):
        grant_stmt = privileges.strip()
    if grant_stmt:
        m = _re.match(
            r'^\s*GRANT\s+(.+?)\s+ON\s+(.+?)\s+TO\s+',
            grant_stmt, _re.IGNORECASE,
        )
        if not m:
            raise MysqlError(
                f'无法解析 GRANT 语句，格式不支持: {grant_stmt[:60]}...')
        priv_part = m.group(1)
        on_part = m.group(2)
        if '.' in on_part:
            gdb, gtable = on_part.split('.', 1)
        else:
            gdb, gtable = on_part, '*'
        gdb = gdb.strip('`').strip() or '*'
        gtable = gtable.strip('`').strip() or '*'
        priv_part_clean = _re.sub(
            r'\s+WITH\s+GRANT\s+OPTION\s*$', '', priv_part,
            flags=_re.IGNORECASE,
        ).strip()
        if _re.match(r'^ALL(\s+PRIVILEGES)?$', priv_part_clean, _re.IGNORECASE):
            privileges = 'ALL PRIVILEGES'
        else:
            privileges = priv_part_clean
        db, table = gdb, gtable
    privs = _normalize_privileges(privileges)
    db_q = quote_ident(db) if db != '*' else '*'
    table_q = quote_ident(table) if table != '*' else '*'
    priv_str = ', '.join(privs)
    sql = f"REVOKE {priv_str} ON {db_q}.{table_q} FROM {quote_ident(user)}@%s"
    try:
        _exec(conn, sql, (host,))
        _exec(conn, 'FLUSH PRIVILEGES')
        conn.commit()
    except MySQLdb.MySQLError as e:
        raise MysqlError(f'撤销授权失败: {e}') from e


def alter_user_password(conn, user: str, host: str, new_password: str):
    """修改指定用户的密码"""
    _validate_user_host(user, host)
    sql = f"ALTER USER {quote_ident(user)}@%s IDENTIFIED BY %s"
    try:
        _exec(conn, sql, (host, new_password))
        conn.commit()
    except MySQLdb.MySQLError as e:
        raise MysqlError(f'修改用户密码失败: {e}') from e


def parse_grant_stmt(grant_stmt: str) -> dict | None:
    """将 SHOW GRANTS 返回的原始 GRANT 语句解析为结构化字典。

    返回: {'privileges': [...], 'priv_str': '...', 'db': '...', 'table': '...',
           'raw': 'GRANT ...', 'is_role': False}
    解析失败（如角色授权）返回 None。
    """
    import re as _re
    s = grant_stmt.strip()
    m = _re.match(
        r'^\s*GRANT\s+(.+?)\s+ON\s+(.+?)\s+TO\s+', s, _re.IGNORECASE)
    if not m:
        return None
    priv_part = m.group(1)
    on_part = m.group(2)
    if '.' in on_part:
        db, table = on_part.split('.', 1)
    else:
        db, table = on_part, '*'
    db = db.strip('`').strip() or '*'
    table = table.strip('`').strip() or '*'
    priv_part_clean = _re.sub(
        r'\s+WITH\s+GRANT\s+OPTION\s*$', '', priv_part,
        flags=_re.IGNORECASE,
    ).strip()
    if _re.match(r'^ALL(\s+PRIVILEGES)?$', priv_part_clean, _re.IGNORECASE):
        privs = ['ALL PRIVILEGES']
        priv_label = 'ALL PRIVILEGES'
    else:
        privs = [p.strip().upper() for p in priv_part_clean.split(',') if p.strip()]
        priv_label = ', '.join(privs)
    scope = 'global' if db == '*' and table == '*' else ('db' if table == '*' else 'table')
    return {
        'privileges': privs,
        'priv_str': priv_label,
        'db': db,
        'table': table,
        'scope': scope,
        'raw': s,
        'can_revoke': not (len(privs) == 1 and privs[0] == 'USAGE'),
    }


def get_user_grants(conn, user: str, host: str) -> list:
    """查询用户拥有的全部权限，返回结构化字典列表"""
    _validate_user_host(user, host)
    sql = f"SHOW GRANTS FOR {quote_ident(user)}@%s"
    cur = _exec(conn, sql, (host,))
    result = []
    for row in cur.fetchall():
        parsed = parse_grant_stmt(row[0])
        if parsed is not None:
            result.append(parsed)
    return result


# ── 表数据浏览 ───────────────────────────────────────────

def _guess_pk_column(conn, db: str, table: str) -> str:
    """猜测表的主键列名（优先 AUTO_INCREMENT，其次第一个 PRIMARY KEY）"""
    cols = list_columns(conn, db, table)
    for c in cols:
        if c.get('extra') and 'auto_increment' in str(c['extra']).lower():
            return c['name']
    for c in cols:
        if c.get('key') == 'PRI':
            return c['name']
    return cols[0]['name'] if cols else ''


def query_table_data(conn, db: str, table: str, page: int = 1,
                     page_size: int = 100) -> dict:
    """分页查询表数据，自动处理大表 OFFSET 降级。

    返回:
        {
            'columns': [字段名列表],
            'rows': [[row1...], [row2...], ...],
            'total': 估算总行数,
            'page': 当前页码,
            'page_size': 每页大小,
            'total_pages': 总页数,
            'has_more': 是否还有下一页,
        }
    """
    db_q, table_q = quote_ident(db), quote_ident(table)
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), 500))

    # 1. 获取列名（SHOW COLUMNS 轻量快速）
    cols = list_columns(conn, db, table)
    col_names = [c['name'] for c in cols]
    pk_col = _guess_pk_column(conn, db, table)

    # 2. 获取总行数（优先用 information_schema 估算，避免 COUNT(*) 全表扫描）
    total = 0
    try:
        cur = _exec(conn,
                    "SELECT Rows FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
                    (db, table))
        row = cur.fetchone()
        if row and row[0] is not None:
            total = int(row[0])
    except Exception:
        pass

    # 估算值为 0 或不可信时，退化到 COUNT(*)
    if total == 0:
        try:
            cur = _exec(conn, f"SELECT COUNT(*) FROM {db_q}.{table_q}")
            row = cur.fetchone()
            total = int(row[0]) if row else 0
        except Exception:
            total = 0

    total_pages = max(1, (total + page_size - 1) // page_size)

    # 3. 大表 OFFSET 降级：OFFSET > 10000 时改用游标分页
    offset = (page - 1) * page_size
    use_cursor = offset > 10000 and pk_col

    if use_cursor:
        cur = _exec(conn, f"SELECT {', '.join(quote_ident(c) for c in col_names)} "
                          f"FROM {db_q}.{table_q} "
                          f"WHERE {quote_ident(pk_col)} > %s "
                          f"ORDER BY {quote_ident(pk_col)} "
                          f"LIMIT %s",
                          (offset, page_size + 1))
    else:
        cur = _exec(conn, f"SELECT {', '.join(quote_ident(c) for c in col_names)} "
                          f"FROM {db_q}.{table_q} "
                          f"LIMIT %s OFFSET %s",
                          (page_size + 1, offset))

    raw_rows = cur.fetchall()
    has_more = len(raw_rows) > page_size
    rows = [list(r) for r in raw_rows[:page_size]]

    return {
        'columns': col_names,
        'rows': rows,
        'total': total,
        'page': page,
        'page_size': page_size,
        'total_pages': total_pages,
        'has_more': has_more,
    }


def show_create_table(conn, db: str, table: str) -> str:
    """获取建表语句（SHOW CREATE TABLE）"""
    db_q, table_q = quote_ident(db), quote_ident(table)
    cur = _exec(conn, f"SHOW CREATE TABLE {db_q}.{table_q}")
    row = cur.fetchone()
    if row:
        return row[1] if len(row) > 1 else str(row[0])
    return ''


# ── 索引管理 ────────────────────────────────────────────

def list_indexes(conn, db: str, table: str) -> list:
    """返回 [{key_name, column_name, non_unique, seq_in_index, index_type}]"""
    db_q, table_q = quote_ident(db), quote_ident(table)
    cur = _exec(conn, f"SHOW INDEX FROM {db_q}.{table_q}")
    cols_info = [d[0] for d in cur.description]
    raw = [dict(zip(cols_info, r)) for r in cur.fetchall()]
    result = []
    for r in raw:
        result.append({
            'key_name': r.get('Key_name', ''),
            'column_name': r.get('Column_name', ''),
            'non_unique': r.get('Non_unique', 1) == 1,
            'seq_in_index': r.get('Seq_in_index', 0),
            'index_type': r.get('Index_type', ''),
        })
    return result


def create_index(conn, db: str, table: str, index_name: str,
                 columns, index_type: str = 'INDEX'):
    """创建索引。columns 可以是字符串或列表"""
    db_q, table_q = quote_ident(db), quote_ident(table)
    idx_q = quote_ident(index_name)
    if isinstance(columns, str):
        columns = [columns]
    col_list = ', '.join(quote_ident(c) for c in columns)
    idx_type = (index_type or 'INDEX').upper()
    if idx_type not in ('INDEX', 'UNIQUE', 'KEY', 'PRIMARY KEY', 'FULLTEXT', 'SPATIAL'):
        raise MysqlError(f'不支持的索引类型: {idx_type}')
    sql = f"CREATE {idx_type} {idx_q} ON {db_q}.{table_q} ({col_list})"
    try:
        _exec(conn, sql)
        conn.commit()
    except MySQLdb.MySQLError as e:
        raise MysqlError(f'创建索引失败: {e}') from e


def drop_index(conn, db: str, table: str, index_name: str):
    """删除索引"""
    db_q, table_q = quote_ident(db), quote_ident(table)
    idx_q = quote_ident(index_name)
    if index_name == 'PRIMARY':
        sql = f"ALTER TABLE {db_q}.{table_q} DROP PRIMARY KEY"
    else:
        sql = f"ALTER TABLE {db_q}.{table_q} DROP INDEX {idx_q}"
    try:
        _exec(conn, sql)
        conn.commit()
    except MySQLdb.MySQLError as e:
        raise MysqlError(f'删除索引失败: {e}') from e