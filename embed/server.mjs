/**
 * MultiChat · 可选本地向量服务
 *
 * 向量算法与缓存格式保持一致：
 *   onnxruntime-web 的 WASM、单线程、一次只算一句、按字平均、L2 归一化。
 *   换模型或推理后端时，应重新验证输出并重建资料向量；不同实现的向量不能默认混用。
 *
 * 用法：node server.mjs <模型目录>
 * 标准输入每行一个请求 {"id":1,"text":"…"}；标准输出每行一个结果 {"id":1,"vec":"<768 个 float32 小端，base64>"}。
 * 第一行输出 {"ready":true} 表示模型加载好了。
 */
import fs from "node:fs";
import path from "node:path";
import readline from "node:readline";
import { fileURLToPath, pathToFileURL } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const modelDir = process.argv[2];
const out = (o) => process.stdout.write(JSON.stringify(o) + "\n");

try {
  const ort = await import(pathToFileURL(path.join(HERE, "node_modules/onnxruntime-web/dist/ort.node.min.mjs")).href);
  ort.env.wasm.numThreads = 1;
  ort.env.logLevel = "error";
  const { Tokenizer } = await import(pathToFileURL(path.join(HERE, "node_modules/@huggingface/tokenizers/dist/tokenizers.mjs")).href);
  const tok = new Tokenizer(
    JSON.parse(fs.readFileSync(path.join(modelDir, "tokenizer.json"), "utf8")),
    JSON.parse(fs.readFileSync(path.join(modelDir, "tokenizer_config.json"), "utf8")),
  );
  const started = Date.now();
  const sess = await ort.InferenceSession.create(
    fs.readFileSync(path.join(modelDir, "onnx/model_quantized.onnx")),
    { executionProviders: ["wasm"] },
  );

  /** 一句话 → 归一化的向量 */
  async function embedOne(text) {
    const ids = tok.encode(text).ids.map(BigInt);
    const n = ids.length;
    const feeds = {};
    for (const name of sess.inputNames) {
      const data = name === "input_ids" ? ids : name === "attention_mask" ? ids.map(() => 1n) : ids.map(() => 0n);
      feeds[name] = new ort.Tensor("int64", BigInt64Array.from(data), [1, n]);
    }
    const h = (await sess.run(feeds))[sess.outputNames[0]].data;
    const d = h.length / n;
    const v = new Float32Array(d);
    for (let i = 0; i < n; i++) for (let k = 0; k < d; k++) v[k] += h[i * d + k] / n;
    let norm = 0;
    for (const x of v) norm += x * x;
    norm = Math.sqrt(norm) || 1;
    for (let k = 0; k < d; k++) v[k] /= norm;
    return v;
  }

  out({ ready: true, ms: Date.now() - started });

  // ⚠️ 一句一句排队算：凑批会改变结果
  const rl = readline.createInterface({ input: process.stdin });
  let chain = Promise.resolve();
  rl.on("line", (line) => {
    chain = chain.then(async () => {
      let req;
      try { req = JSON.parse(line); } catch { return; }
      try {
        const v = await embedOne(String(req.text ?? ""));
        out({ id: req.id, vec: Buffer.from(v.buffer, v.byteOffset, v.byteLength).toString("base64") });
      } catch (e) {
        out({ id: req.id, error: String(e?.message ?? e) });
      }
    });
  });
  rl.on("close", () => { chain.then(() => process.exit(0)); });
} catch (e) {
  out({ ready: false, error: String(e?.message ?? e) });
  process.exit(1);
}
