# 文件支持矩阵

DongJian 的一级目录单位是源文件。一个文件可以在内部产生多个独立的
`TableAsset`、`TextAsset` 和 `TextChunk`，但 Catalog 默认按一个文件展示。

| 类别 | 格式 | 当前行为 |
| --- | --- | --- |
| 正式深度处理 | CSV / TSV | 本地表格提取、清洗、Catalog、Search、Safe SQL |
| 正式深度处理 | XLS / XLSX | 本地 Calamine/Polars 表格提取、清洗、Catalog、Search、Safe SQL |
| 正式深度处理 | PDF | PyMuPDF 逐页识别；原生文本本地提取；表格先作为候选进入表格路线；文件详情按页展示有界正文和表格预览；扫描页按设置选择本地 OCR 或显式 Vision |
| 正式深度处理 | DOCX | 标准库 OOXML 读取段落和内嵌表格；不依赖 Word/Office |
| 正式深度处理 | TXT | 本地文本提取、分块、Search |
| 正式深度处理 | JPG / JPEG / PNG | 本地 OCR；img2table 结果先作为候选表格并做保守空值/重复过滤；文件详情展示 OCR 文本和候选预览；显式选择且 Vision 已配置时可使用 Vision |
| 下一阶段 | PPTX | 仅登记并标记为暂不支持深度处理 |
| 下一阶段 | HTML / XML | 仅登记并标记为暂不支持深度处理 |
| 下一阶段 | DOC | 仅登记并标记为暂不支持深度处理 |
| Catalog only / skip | CSS / ZIP / `.Identifier` / `.下载` / unknown binary | 保留 Registry 记录，不执行深度提取，不把它们视为批处理失败 |

所有提取均写入项目内 `workspace/`，不修改源文件。缺少 AI 配置时，所有
本地提取、清洗、Catalog、Search 和 Safe SQL 仍保持可用；模型请求不会被
隐式触发。文件详情是主要内容查看页，但复杂 PDF/图片版面仍是 best-effort，
候选表格不等于人工确认的正确表格。
