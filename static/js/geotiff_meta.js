/**
 * GeoTIFF 头部读取器：从浏览器里的 File 对象读出第一个 IFD 的地理元信息，
 * **不上传、不读像素**。
 *
 * 为什么要在前端读：高程切片的输入是 <input type="file"> 选中的本地 tif，
 * 动辄几百 MB 到 2 GB。要在「选完文件」这一刻就给出坐标系/范围/分辨率，
 * 唯一不付出一次整包上传代价的办法就是自己走 TIFF 的目录结构 —— 用
 * File.slice() 按需取几个 KB（浏览器对本地文件是按范围读的，不会整包载入）。
 *
 * 为什么只做「读」不做「解释」：EPSG 码 → 坐标系名称、投影坐标 → WGS84
 * 经纬度，这些要一份完整的 CRS 库才做得对（国内 DEM 常见 CGCS2000 高斯
 * 克吕格分带）。后端本来就带着 GDAL/osr，解释放在 /api/raster/inspect，
 * 前端只把原始标签值发过去。所以这个文件里没有任何 EPSG 常识。
 *
 * 覆盖范围：经典 TIFF（magic 42）与 BigTIFF（magic 43，>4GB 的 DEM 常用），
 * 大小端都支持。只读 IFD0 —— 概览金字塔在后续 IFD 里，与「源数据有效信息」无关。
 */
'use strict';

(function (global) {

    // 按需取值的标签白名单。**必须是白名单**：StripOffsets/StripByteCounts
    // 这类数组在大文件里是几 MB，全量解析等于把「只读几 KB」的前提作废。
    const TAG = {
        IMAGE_WIDTH: 256,
        IMAGE_LENGTH: 257,
        BITS_PER_SAMPLE: 258,
        COMPRESSION: 259,
        SAMPLES_PER_PIXEL: 277,
        ROWS_PER_STRIP: 278,
        PLANAR_CONFIG: 284,
        TILE_WIDTH: 322,
        TILE_LENGTH: 323,
        SAMPLE_FORMAT: 339,
        MODEL_PIXEL_SCALE: 33550,
        MODEL_TIEPOINT: 33922,
        MODEL_TRANSFORMATION: 34264,
        GEO_KEY_DIRECTORY: 34735,
        GEO_DOUBLE_PARAMS: 34736,
        GEO_ASCII_PARAMS: 34737,
        GDAL_METADATA: 42112,
        GDAL_NODATA: 42113,
    };
    const WANTED = new Set(Object.keys(TAG).map((k) => TAG[k]));

    // TIFF 字段类型 -> 单元字节数。16/17/18 是 BigTIFF 新增的 8 字节类型。
    const TYPE_SIZE = {
        1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2,
        9: 4, 10: 8, 11: 4, 12: 8, 13: 4, 16: 8, 17: 8, 18: 8,
    };

    const CHUNK = 65536;
    // 单个标签值的读取上限：GDAL_METADATA 是一段 XML，正常几百字节；
    // 损坏文件里 count 可能是天文数字，别让一次误读吃掉几百 MB 内存。
    const MAX_VALUE_BYTES = 1 << 20;
    // IFD 条目数上限。同上，是防「把长度字段当真」的护栏，不是格式限制
    // （真实 GeoTIFF 的 IFD0 条目数在 20 上下）。
    const MAX_ENTRIES = 4096;

    /** File 的按块缓存读取。同一个块只切一次，避免每个标签都走一次 slice。 */
    function createReader(file) {
        const cache = new Map();
        async function chunk(index) {
            let buf = cache.get(index);
            if (!buf) {
                const start = index * CHUNK;
                const blob = file.slice(start, Math.min(start + CHUNK, file.size));
                buf = new Uint8Array(await blob.arrayBuffer());
                cache.set(index, buf);
            }
            return buf;
        }
        return async function read(offset, length) {
            if (!Number.isFinite(offset) || !Number.isFinite(length)
                || offset < 0 || length < 0 || offset + length > file.size) {
                throw new RangeError(`out of range: ${offset}+${length} of ${file.size}`);
            }
            const out = new Uint8Array(length);
            let done = 0;
            while (done < length) {
                const abs = offset + done;
                const index = Math.floor(abs / CHUNK);
                const buf = await chunk(index);
                const from = abs - index * CHUNK;
                const n = Math.min(length - done, buf.length - from);
                if (n <= 0) throw new RangeError('unexpected end of file');
                out.set(buf.subarray(from, from + n), done);
                done += n;
            }
            return out;
        };
    }

    function view(bytes) {
        return new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    }

    /** 8 字节偏移量 -> Number。TIFF 偏移不会超过 2^53，转换是精确的。 */
    function u64(dv, at, le) {
        return Number(dv.getBigUint64(at, le));
    }

    /** 按 TIFF 字段类型把原始字节解成数值数组；ASCII(2) 解成字符串。 */
    function decode(type, count, bytes, le) {
        const dv = view(bytes);
        if (type === 2) {
            let s = '';
            for (let i = 0; i < count && i < bytes.length; i++) {
                const c = bytes[i];
                if (c === 0) { s += '\0'; continue; }  // 多段 ASCII 用 NUL 分隔，留着给调用方切
                s += String.fromCharCode(c);
            }
            return s;
        }
        const out = new Array(count);
        for (let i = 0; i < count; i++) {
            switch (type) {
                case 1: case 7: out[i] = dv.getUint8(i); break;
                case 6: out[i] = dv.getInt8(i); break;
                case 3: out[i] = dv.getUint16(i * 2, le); break;
                case 8: out[i] = dv.getInt16(i * 2, le); break;
                case 4: case 13: out[i] = dv.getUint32(i * 4, le); break;
                case 9: out[i] = dv.getInt32(i * 4, le); break;
                case 11: out[i] = dv.getFloat32(i * 4, le); break;
                case 12: out[i] = dv.getFloat64(i * 8, le); break;
                case 5: out[i] = dv.getUint32(i * 8, le) / (dv.getUint32(i * 8 + 4, le) || 1); break;
                case 10: out[i] = dv.getInt32(i * 8, le) / (dv.getInt32(i * 8 + 4, le) || 1); break;
                case 16: case 18: out[i] = u64(dv, i * 8, le); break;
                case 17: out[i] = Number(dv.getBigInt64(i * 8, le)); break;
                default: return null;   // 未知类型：不猜
            }
        }
        return out;
    }

    /**
     * GeoKeyDirectory(34735) 展开成 {keyId: value}。
     * 目录头是 4 个 short，之后每 4 个 short 一条：
     * (keyId, tagLocation, count, valueOrOffset)。tagLocation 为 0 时值就在
     * valueOrOffset 里；为 34736/34737 时是到 double/ascii 参数数组的下标。
     */
    function expandGeoKeys(dir, doubles, ascii) {
        if (!dir || dir.length < 4) return null;
        const keys = {};
        const n = Math.min(dir[3], Math.floor((dir.length - 4) / 4));
        for (let i = 0; i < n; i++) {
            const at = 4 + i * 4;
            const id = dir[at];
            const loc = dir[at + 1];
            const count = dir[at + 2];
            const value = dir[at + 3];
            if (loc === 0) {
                keys[id] = value;
            } else if (loc === TAG.GEO_DOUBLE_PARAMS && doubles) {
                if (value < doubles.length) keys[id] = doubles[value];
            } else if (loc === TAG.GEO_ASCII_PARAMS && typeof ascii === 'string') {
                // GeoTIFF 规范用 '|' 当 ASCII 段终止符
                keys[id] = ascii.substr(value, count).replace(/[|\0]+$/, '');
            }
        }
        return keys;
    }

    /** GDAL_METADATA(42112) 里的统计量。GDAL 算过统计的文件才有，没有就是没有。 */
    function parseGdalStatistics(xml) {
        if (typeof xml !== 'string') return null;
        const pick = (name) => {
            const m = xml.match(new RegExp(`name="${name}"[^>]*>([^<]+)<`));
            const v = m ? parseFloat(m[1]) : NaN;
            return Number.isFinite(v) ? v : null;
        };
        const min = pick('STATISTICS_MINIMUM');
        const max = pick('STATISTICS_MAXIMUM');
        return (min === null || max === null) ? null : { min, max };
    }

    /**
     * 读一个 File 的 GeoTIFF 头部。
     * 成功返回原始标签值（不做地理解释），失败抛 Error。
     */
    async function read(file) {
        // 不够一个 TIFF 头（8 字节，BigTIFF 16 字节）就不是 TIFF。不先挡一下的话
        // 下面那次 readAt(0, 16) 抛的是 RangeError: out of range，控制台里看着
        // 像解析器坏了，其实只是选了个空文件。
        if (file.size < 16) throw new Error('not a TIFF file');
        const readAt = createReader(file);
        const head = view(await readAt(0, 16));

        const bom = (head.getUint8(0) << 8) | head.getUint8(1);
        let le;
        if (bom === 0x4949) le = true;          // 'II'
        else if (bom === 0x4d4d) le = false;    // 'MM'
        else throw new Error('not a TIFF file');

        const magic = head.getUint16(2, le);
        let big = false;
        let ifdOffset;
        if (magic === 42) {
            ifdOffset = head.getUint32(4, le);
        } else if (magic === 43) {
            big = true;
            if (head.getUint16(4, le) !== 8) throw new Error('unsupported BigTIFF offset size');
            ifdOffset = u64(head, 8, le);
        } else {
            throw new Error('not a TIFF file');
        }

        const countSize = big ? 8 : 2;
        const entrySize = big ? 20 : 12;
        const inlineSize = big ? 8 : 4;

        const countBytes = view(await readAt(ifdOffset, countSize));
        const entryCount = big ? u64(countBytes, 0, le) : countBytes.getUint16(0, le);
        if (entryCount <= 0 || entryCount > MAX_ENTRIES) {
            throw new Error(`implausible IFD entry count: ${entryCount}`);
        }

        const block = await readAt(ifdOffset + countSize, entryCount * entrySize);
        const dv = view(block);

        // 先把感兴趣的条目挑出来，再统一取外置值：这样最多只发生
        // 「白名单条目数」次范围读，而不是每个条目都读。
        const pending = [];
        for (let i = 0; i < entryCount; i++) {
            const at = i * entrySize;
            const tag = dv.getUint16(at, le);
            if (!WANTED.has(tag)) continue;
            const type = dv.getUint16(at + 2, le);
            const count = big ? u64(dv, at + 4, le) : dv.getUint32(at + 4, le);
            const size = TYPE_SIZE[type];
            if (!size) continue;
            const total = size * count;
            if (total > MAX_VALUE_BYTES) continue;
            const valueAt = at + (big ? 12 : 8);
            if (total <= inlineSize) {
                pending.push({ tag, type, count, bytes: block.subarray(valueAt, valueAt + total) });
            } else {
                const offset = big ? u64(dv, valueAt, le) : dv.getUint32(valueAt, le);
                pending.push({ tag, type, count, offset, total });
            }
        }

        const values = {};
        for (const e of pending) {
            const bytes = e.bytes || await readAt(e.offset, e.total);
            const decoded = decode(e.type, e.count, bytes, le);
            if (decoded !== null) values[e.tag] = decoded;
        }

        const num = (tag) => {
            const v = values[tag];
            return Array.isArray(v) && v.length ? v[0] : null;
        };
        const arr = (tag) => (Array.isArray(values[tag]) ? values[tag] : null);
        const str = (tag) => (typeof values[tag] === 'string' ? values[tag] : null);

        const width = num(TAG.IMAGE_WIDTH);
        const height = num(TAG.IMAGE_LENGTH);
        if (!width || !height) throw new Error('missing image dimensions');

        const nodataRaw = str(TAG.GDAL_NODATA);
        const nodata = nodataRaw === null ? null : parseFloat(nodataRaw.replace(/\0.*$/, ''));

        return {
            name: file.name,
            size: file.size,
            big_tiff: big,
            width,
            height,
            samples: num(TAG.SAMPLES_PER_PIXEL) || 1,
            bits: num(TAG.BITS_PER_SAMPLE) || 8,
            sample_format: num(TAG.SAMPLE_FORMAT) || 1,
            compression: num(TAG.COMPRESSION) || 1,
            tile_width: num(TAG.TILE_WIDTH),
            tile_height: num(TAG.TILE_LENGTH),
            rows_per_strip: num(TAG.ROWS_PER_STRIP),
            pixel_scale: arr(TAG.MODEL_PIXEL_SCALE),
            tie_point: arr(TAG.MODEL_TIEPOINT),
            transform: arr(TAG.MODEL_TRANSFORMATION),
            geo_keys: expandGeoKeys(
                arr(TAG.GEO_KEY_DIRECTORY),
                arr(TAG.GEO_DOUBLE_PARAMS),
                str(TAG.GEO_ASCII_PARAMS),
            ),
            nodata: Number.isFinite(nodata) ? nodata : null,
            statistics: parseGdalStatistics(str(TAG.GDAL_METADATA)),
        };
    }

    global.GeoTiffMeta = { read: read, TAG: TAG };

})(typeof globalThis !== 'undefined' ? globalThis : window);
