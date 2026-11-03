# 来源与第三方许可

框架从作者的多会话实现提取，保留会话调度、记忆、状态、工具循环和仪表盘机制。框架源代码及随仓库提供的虚构示例采用 [MIT License](LICENSE)，版权声明见根目录许可证。

框架中知识、人物、语气和身份资料均为精简的虚构样例，不含原角色完整剧情资料、密文、解密密钥、原图标、表情图片或私人种子数据。

可选向量服务沿用裁剪后的运行库，固定版本和完整文件说明见 [embed/NOTICE.md](embed/NOTICE.md)：

| 组件 | 随包许可位置 |
| --- | --- |
| onnxruntime-web | `embed/node_modules/onnxruntime-web/LICENSE`、`ThirdPartyNotices.txt` |
| onnxruntime-common | `embed/node_modules/onnxruntime-common/LICENSE`、`ThirdPartyNotices.txt` |
| @huggingface/tokenizers | `embed/node_modules/@huggingface/tokenizers/LICENSE` |

可选本地向量接口配置的模型为 Jina AI 的 [jina-embeddings-v2-base-zh](https://huggingface.co/jinaai/jina-embeddings-v2-base-zh)，官方模型卡标注采用 [Apache-2.0](https://www.apache.org/licenses/LICENSE-2.0)。模型由 Jina AI 提供，不是本框架训练的模型；本仓库不附带模型权重，ONNX 文件来源和接入说明见 [embed/NOTICE.md](embed/NOTICE.md)。

NumPy、可选 Node.js、宿主平台 SDK 以及用户自行提供的向量模型遵循各自项目许可。上述第三方许可只适用于相应组件，不代表本框架所有文件采用相同许可。
