"""将 BMP 格式的 .ico 文件转换为标准 ICO 格式（纯 Python，无第三方依赖）"""
import struct
import os
import sys


def bmp_to_ico(bmp_path, ico_path=None):
    """读取 BMP 文件，生成标准 ICO 文件"""
    if ico_path is None:
        ico_path = bmp_path

    with open(bmp_path, 'rb') as f:
        bmp_data = f.read()

    # 验证 BMP 头
    if bmp_data[:2] != b'BM':
        print(f'  跳过 {bmp_path}: 不是 BMP 格式')
        return False

    # 解析 BMP 头
    pix_offset = struct.unpack_from('<I', bmp_data, 10)[0]
    dib_size = struct.unpack_from('<I', bmp_data, 14)[0]
    width = struct.unpack_from('<i', bmp_data, 18)[0]
    height = struct.unpack_from('<i', bmp_data, 22)[0]
    bpp = struct.unpack_from('<H', bmp_data, 28)[0]

    print(f'  BMP: {width}x{height}, {bpp}bpp, DIB头={dib_size}字节, 像素偏移={pix_offset}')

    # 提取 DIB 数据（去掉 14 字节的 BMP 文件头）
    dib_data = bytearray(bmp_data[14:])

    # ICO 要求：DIB 头中的 height 字段需要翻倍（为 AND 掩码预留空间）
    struct.pack_into('<i', dib_data, 8, height * 2)

    # 计算 XOR 掩码大小（像素数据）
    xor_size = len(bmp_data) - pix_offset

    # 32bpp 时 alpha 通道已包含透明度，AND 掩码全零即可
    # AND 掩码大小 = 对齐到 32 位边界的行宽 * 高度
    and_mask_row = ((width + 31) // 32) * 4  # 每行对齐到 4 字节
    and_mask_size = and_mask_row * height
    and_mask = b'\x00' * and_mask_size

    # 完整的图像数据 = DIB 头 + 颜色表 + 像素数据 + AND 掩码
    image_data = bytes(dib_data) + and_mask

    # 构建 ICO 文件
    ico_header = struct.pack('<HHH', 0, 1, 1)  # Reserved=0, Type=1(ICO), Count=1

    # ICO 目录条目（16 字节）
    dir_entry = struct.pack(
        '<BBBBHHII',
        width if width < 256 else 0,     # 宽度（0 表示 256）
        height if height < 256 else 0,    # 高度
        0,                                 # 颜色数（0 = 不指定）
        0,                                 # 保留
        1,                                 # 颜色平面
        bpp,                               # 位深度
        len(image_data),                   # 图像数据大小
        6 + 16                             # 偏移量（Header 6 + 1个目录条目 16）
    )

    ico_data = ico_header + dir_entry + image_data

    with open(ico_path, 'wb') as f:
        f.write(ico_data)

    print(f'  已转换: {ico_path} ({len(ico_data)} 字节)')
    return True


def verify_ico(path):
    """验证 ICO 文件格式"""
    with open(path, 'rb') as f:
        data = f.read(6)
    reserved, itype, count = struct.unpack('<HHH', data)
    ok = (reserved == 0 and itype == 1 and count > 0)
    status = '合法 ICO' if ok else '非法格式'
    print(f'  验证 {path}: {status} (Reserved={reserved}, Type={itype}, Count={count})')
    return ok


if __name__ == '__main__':
    base_dir = os.path.dirname(os.path.abspath(__file__))  # jieenv/
    ico_files = [
        os.path.join(os.path.dirname(base_dir), 'favicon.ico'),  # 根目录
        os.path.join(base_dir, 'jiecore', 'static', 'favicon.ico'),
        os.path.join(base_dir, 'apps', 'mainapp', 'static', 'favicon.ico'),
    ]

    # 先验证当前状态
    print('=== 转换前验证 ===')
    for p in ico_files:
        if os.path.exists(p):
            verify_ico(p)
        else:
            print(f'  文件不存在: {p}')

    # 转换
    print('\n=== 开始转换 ===')
    for p in ico_files:
        if os.path.exists(p):
            print(f'\n处理: {p}')
            bmp_to_ico(p)

    # 转换后验证
    print('\n=== 转换后验证 ===')
    for p in ico_files:
        if os.path.exists(p):
            verify_ico(p)

    print('\n转换完成!')
