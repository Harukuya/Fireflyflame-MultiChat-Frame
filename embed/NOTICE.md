# 向量服务用到的第三方文件

本目录只包含本地向量服务必需的裁剪运行文件，固定版本如下：

| 包 | 版本 | 许可 | 拷了哪些 |
|---|---|---|---|
| onnxruntime-web | 1.31.0-dev.20260914-8d85527a0 | MIT（Microsoft） | `dist/ort.node.min.mjs`、`dist/ort-wasm-simd-threaded.mjs`、`dist/ort-wasm-simd-threaded.wasm` |
| onnxruntime-common | 1.31.0-dev.20260911-2a43ec07e | MIT（Microsoft） | `dist/esm/` |
| @huggingface/tokenizers | 0.2.0 | Apache-2.0 | `dist/tokenizers.mjs`（LICENSE 在包目录里） |

服务直接使用 `@huggingface/tokenizers` 0.2.0，不需要安装完整的 Transformers.js。本框架只提供运行文件，不包含模型权重或预计算资料向量；当前精简样例默认按字面检索，离线验收未加载真实向量模型。

公开发布副本补齐 ONNX 两个包目录内的 `LICENSE` 和 `ThirdPartyNotices.txt`，分别来自所用固定提交：

- [onnxruntime-web：8d85527a0](https://github.com/microsoft/onnxruntime/tree/8d85527a0)
- [onnxruntime-common：2a43ec07e](https://github.com/microsoft/onnxruntime/tree/2a43ec07e)

这些上游许可与声明适用于对应依赖，并原样保留；本框架自身采用根目录的 [MIT License](../LICENSE)。

## 向量模型来源

`multichat/embed.py` 的模型标识为 `jinaai/jina-embeddings-v2-base-zh`，由 Jina AI 提供，采用 [Apache-2.0](https://www.apache.org/licenses/LICENSE-2.0)。官方模型仓库及说明：[Hugging Face · jinaai/jina-embeddings-v2-base-zh](https://huggingface.co/jinaai/jina-embeddings-v2-base-zh)。

当前本地服务读取 `tokenizer.json`、`tokenizer_config.json` 和 `onnx/model_quantized.onnx`，该量化文件可在官方仓库的 [onnx 目录](https://huggingface.co/jinaai/jina-embeddings-v2-base-zh/tree/main/onnx) 获取；服务使用 mean pooling 与 L2 归一化。本框架不附带权重或预计算资料向量，默认虚构样例使用字面检索，启用向量检索需另行准备模型及与它兼容的资料向量。模型的许可独立于框架代码的 MIT 许可。
