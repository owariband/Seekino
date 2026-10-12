# MySQL 8.4.11 JSON 向量浮点精度研究（2026-10-12）

结论：本例的 1 ULP 差异在 **JSON 十进制数字输入解析**阶段即可复现。MySQL 8.4.11 默认调用的 RapidJSON 路径没有开启 full precision；二进制 JSON 保存的是八字节 double，并非缩成 float32。读出的不同尾数可以是已改变的 double 的正常十进制表示，不能仅看输出文本就归因于读出格式化。

## 版本、范围与证据等级

- MySQL 源码固定到官方 `mysql/mysql-server` 的 `mysql-8.4.11` tag；官方发布日期为 2026-07-28。[发布说明](https://dev.mysql.com/doc/relnotes/mysql/8.4/en/news-8-4-11.html)
- MySQL 机制依据仅使用官方手册和该 tag 的源码；ES 边界使用官方 8.19 文档及 `v8.19.22` 源码。
- **已证实**包括可直接读取的源码、同 tag 解析器的独立实验，以及本轮直接读取的 ES settings。
- **源码推断**指这些实现如何解释此前项目观察；它不等于已对运行中的 MySQL 函数逐步调试。
- **未证实**包括历史各次模型调用的原始响应与对应写入者。本轮 MySQL 没有监听，未启动服务、调用模型、写数据库或修改业务。

## 三个环节分别做了什么

| 环节 | 8.4.11 实现 | 结论与边界 |
| --- | --- | --- |
| 文本 JSON → DOM | `Json_dom::parse()` 使用 RapidJSON `kParseDefaultFlags` | 默认数字解析能产生本例的邻近 binary64 值。 |
| DOM → binary JSON → DOM | `J_DOUBLE` 经 `float8store`/`float8get` 保存、读取 | 保存已解析 double 的八字节表示；没有 float32 缩窄。 |
| double → JSON 文本 | `my_gcvt` 使用 DOUBLE 参数与最大字段宽度 | 格式化已存在的 double；不是固定截成 15 位小数。 |

官方手册说明 JSON 列采用内部二进制形式，读单个元素时无需从整段文本重新解析。[JSON 类型](https://dev.mysql.com/doc/refman/8.4/en/json.html)
具体路径依据见下列源码；手册概述本身不能证明十进制转 double 的每次舍入都正确。

### 已证实：默认解析未开启 full precision

`Json_dom::parse()` 在第 589 行调用默认 flags；`Double(double)` 回调直接构造 `Json_double`。[json_dom.cc](https://github.com/mysql/mysql-server/blob/mysql-8.4.11/sql-common/json_dom.cc#L496-L506)、[解析调用](https://github.com/mysql/mysql-server/blob/mysql-8.4.11/sql-common/json_dom.cc#L582-L600)
vendored `reader.h` 中默认宏为 `kParseNoFlags`，full precision 是单独的 flag 16；数字解析分别进入 `StrtodNormalPrecision` 或 `StrtodFullPrecision`。[flags](https://github.com/mysql/mysql-server/blob/mysql-8.4.11/extra/rapidjson/include/rapidjson/reader.h#L133-L158)、[分支](https://github.com/mysql/mysql-server/blob/mysql-8.4.11/extra/rapidjson/include/rapidjson/reader.h#L1707-L1726)
普通路径用有限精度 significand 与十的幂进行浮点运算；full 路径保留十进制字符并在需要时进行更精细的转换。[普通路径](https://github.com/mysql/mysql-server/blob/mysql-8.4.11/extra/rapidjson/include/rapidjson/internal/strtod.h#L28-L46)、[full 路径](https://github.com/mysql/mysql-server/blob/mysql-8.4.11/extra/rapidjson/include/rapidjson/internal/strtod.h#L229-L300)
MySQL 默认构建选择 bundled RapidJSON，也支持 system 依赖；该 CMake 文件没有把默认解析宏改为 full precision。[构建配置](https://github.com/mysql/mysql-server/blob/mysql-8.4.11/cmake/rapidjson.cmake#L82-L143)
运行二进制是否存在发行版补丁或额外编译宏，本轮未读取构建产物验证；这里的实验明确针对官方 bundled 版本。

### 已证实：独立解析实验已隔离输入阶段

从官方 tag 下载 bundled headers，在 `/private/tmp/anivag-mysql-json-precision/` 用 `clang++ -std=c++17 -O0` 编译 Reader 实验；没有 MySQL、数据库存储或 `my_gcvt` 参与。
比较相同输入的默认 flag、full flag，以及本机 `strtod` 的十六进制 double 值。实验打印 `default_flags=0`、`full_flag=16`。

| 输入十进制 | 参考 binary64 | 默认解析 | 默认误差 | full 与参考相同 |
| --- | --- | --- | ---: | --- |
| `9.059906005859375e-05` | `0x1.7cp-14` | `0x1.7c00000000001p-14` | +1 ULP | 是 |
| `0.00009059906005859375` | `0x1.7cp-14` | `0x1.7c00000000001p-14` | +1 ULP | 是 |
| `-0.00010395050048828125` | `-0x1.b4p-14` | `-0x1.b3fffffffffffp-14` | +1 ULP | 是 |
| `1.9311904907226562e-05` | `0x1.44p-16` | `0x1.43fffffffffffp-16` | −1 ULP | 是 |
| `-0.00011348724365234375` | `-0x1.dcp-14` | `-0x1.dc00000000001p-14` | −1 ULP | 是 |
| `0.01544189453125`、`0.1`、`5e-324` | 各自参考值 | 各自参考值 | 0 | 是 |

前两个输入仅书写形式不同，默认路径都产生同一个邻近值；这排除了“只有科学计数法展示变化”的解释。
结果证明这些故障小数在输入解析时已经改变，不需要经过磁盘存储或读出格式化才能出现差异。
它不证明所有 JSON 数字都会改变，也不证明普通路径对任意输入的误差上界恰好为 1 ULP。

最小复现主体如下；include 指向同 tag 的 `extra/rapidjson/include`，不要换成未知版本的系统 headers。

```cpp
#include <rapidjson/reader.h>
#include <rapidjson/memorystream.h>
#include <cstdio>
#include <cstring>
struct H : rapidjson::BaseReaderHandler<rapidjson::UTF8<>, H> {
  double value = 0;
  bool Double(double v) { value = v; return true; }
};
template<unsigned F> double parse(const char *s) {
  rapidjson::MemoryStream in(s, std::strlen(s));
  rapidjson::Reader reader; H h;
  if (!reader.Parse<F>(in, h)) return 0;
  return h.value;
}
int main() {
  const char *s = "9.059906005859375e-05";
  std::printf("default=%a full=%a\n",
    parse<rapidjson::kParseDefaultFlags>(s),
    parse<rapidjson::kParseFullPrecisionFlag>(s));
}
```

```sh
clang++ -std=c++17 -O0 -I<mysql-8.4.11>/extra/rapidjson/include parser.cpp -o parser
./parser
# default=0x1.7c00000000001p-14 full=0x1.7cp-14
```

### 已证实：binary JSON double 没有缩窄成 float32

`json_binary.cc` 为 double 分配八字节，以 `float8store` 写入，并在读取时用 `float8get` 恢复。[写入](https://github.com/mysql/mysql-server/blob/mysql-8.4.11/sql-common/json_binary.cc#L833-L841)、[读取](https://github.com/mysql/mysql-server/blob/mysql-8.4.11/sql-common/json_binary.cc#L969-L972)
因此源码支持的链路是“输入已经解析成 A′，binary JSON 保存 A′”；不能据 1 ULP 现象声称存储将 double 变成了 float32。
该结论针对 JSON 的 double 分支；MySQL JSON 还存在整数、decimal 等类型，并非所有数字都统一采用 double。

### 源码推断：读出字符串呈现了已改变的值

JSON `J_DOUBLE` 序列化调用 `my_gcvt(d, MY_GCVT_ARG_DOUBLE, MY_GCVT_MAX_FIELD_WIDTH, ...)`。[JSON 序列化](https://github.com/mysql/mysql-server/blob/mysql-8.4.11/sql-common/json_dom.cc#L1576-L1597)
`my_gcvt` 使用 dtoa mode 4；只有 FLOAT 参数才走 `FLT_DIG` 限制，DOUBLE 使用字段宽度。最大宽度为 binary64 的有效数字、符号及指数等预留空间。[my_gcvt](https://github.com/mysql/mysql-server/blob/mysql-8.4.11/strings/dtoa.cc#L304-L326)、[宽度](https://github.com/mysql/mysql-server/blob/mysql-8.4.11/include/mysql/strings/dtoa.h#L32-L49)
dtoa mode 4 在 round-nearest 条件下可以返回仍舍入回同一 double 的较短字符串；这与“固定砍掉尾数”是不同的机制。[dtoa 模式](https://github.com/mysql/mysql-server/blob/mysql-8.4.11/strings/dtoa.cc#L1934-L1953)
官方手册亦说明 dtoa 的浮点→字符串→浮点往返性质，但不能把该一般转换说明外推为 RapidJSON 默认输入算法的保证。[类型转换](https://dev.mysql.com/doc/refman/8.4/en/type-conversion.html)
本例 `0.00009059906005859376` 对应相邻 double：普通解析已经生成这个值，后续输出它不需要额外舍入错误。
本轮未独立编译整个 MySQL 的 `my_gcvt` 或调试离线服务器，故不把所有读出路径的正确性宣布为已实测。

## 数值影响与工程影响分开评价

对 `x=-0.00010395050048828125`、`y=-0.00010395050048828124`，本机纯计算得到 `|y-x|=1.3552527156068805e-20`，恰好 1 ULP；相对差为 `1.3037481390093581e-16`。
两者转换成 float32 后位模式相同（大端字节 `b8da0000`）。这是该坐标的结果，不是任意 binary64 相邻值都转成相同 float32 的定理。
本线程辅助实验选取五个真实 1024 维向量，每个构造五种逐坐标至多 1 ULP 的扰动（零值可不移动）：norm 最大变化 `2.220446049250313e-16`；250 组 cosine 比较最大变化 `1.1102230246251565e-16`。
25,595 次非零分量比较的 float32 位模式全部相同；5 次零分量比较中两次只改变正负零符号位，数值仍相等。该有限样本不能证明全部数据、排序边界或所有转换都无影响。
这些变化量不足以解释项目中数百维、约 `0.0068` 的大差；大差的历史写入来源仍未关联到具体模型调用。
即使相似度影响很小，逐元素严格相等、哈希、完整 source 对比仍会区分 A 与 A′。这属于精确重放合同的敏感性，不能据此声称检索语义已经明显改变。

## ES 边界与当前只读快照

ES 的原始 `_source` 与内部向量表示需要区分：官方源码的常规 source 保存输入字节，无过滤时直接返回；dense vector 内部按 float32 解析、索引。[SourceFieldMapper](https://github.com/elastic/elasticsearch/blob/v8.19.22/server/src/main/java/org/elasticsearch/index/mapper/SourceFieldMapper.java#L419)、[DenseVectorFieldMapper](https://github.com/elastic/elasticsearch/blob/v8.19.22/server/src/main/java/org/elasticsearch/index/mapper/DenseVectorFieldMapper.java#L610)
synthetic source 才可能从索引结构重建数值；不能把内部 float32 等同于常规 `_source` 的文本精度。[8.19 source 文档](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/mapping-source-field.html)
2026-10-12 根任务对实际索引执行只读 `_settings?flat_settings=true&include_defaults=true`，版本为 8.19.22；相关显式 settings 均未设置，返回默认值如下：

```json
{"index.mode":"standard","index.mapping.source.mode":"STORED",
 "index.default_pipeline":"_none","index.final_pipeline":"_none"}
```

同次只读 mapping 返回 `_source={}`，没有 includes/excludes；两路向量均为 1024 维 cosine、HNSW（`m=16`、`ef_construction=100`），未采用 int8 类型，索引合同为 `anivag-video-chunk-es-6`。
因此当前实际索引已证实使用原始 stored source 路径，排除 synthetic source、source 过滤和配置的 ingest pipeline；这份快照不证明所有历史写入时刻的配置都相同。

## 归因与后续验证边界

1. **已证实**：官方同 tag 默认 parser 可复现故障坐标的 1 ULP；full flag 对本组恢复参考值；binary JSON double 是八字节。
2. **有力源码推断**：此前 MySQL JSON 转换输出与该默认 parser 的结果吻合，输入解析是具体且可复现的解释；无需假设存储缩窄或读出固定截位。
3. **未证实**：历史每次模型响应内存值、MySQL 缓存、ES 文档是否来自同一次调用，以及大差的具体写入者。ES 客户端纯 dumps/loads 对故障值保留 bits，也不能补齐这条历史关联链。
4. JSON 字符串封装使数据库 parser 把内层数字当文字，能避开这次数值解析边界；另设 JSON 列而仍写原生数字数组，本身不会改变解析算法。[String 回调](https://github.com/mysql/mysql-server/blob/mysql-8.4.11/sql-common/json_dom.cc#L450-L472)
5. 数值精度、缓存归属、同版本并发覆盖、索引版本更新规则是不同问题。此次研究没有修改其中任何业务策略。

本轮仅新增这份研究文档；源码与可执行实验均保存在临时目录，没有启动 MySQL、调用模型或修改历史缓存。
