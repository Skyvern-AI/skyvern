import express from "express";
import fs from "fs";
import cors from "cors";

const app = express();

app.use(cors());

app.get("/artifact/recording", (req, res) => {
  const range = req.headers.range;
  const path = req.query.path;
  const videoSize = fs.statSync(path).size;
  const chunkSize = 1 * 1e6;
  const start = Number(range.replace(/\D/g, ""));
  const end = Math.min(start + chunkSize, videoSize - 1);
  const contentLength = end - start + 1;
  const headers = {
    "Content-Range": `bytes ${start}-${end}/${videoSize}`,
    "Accept-Ranges": "bytes",
    "Content-Length": contentLength,
    "Content-Type": "video/mp4",
  };
  res.writeHead(206, headers);
  const stream = fs.createReadStream(path, {
    start,
    end,
  });
  stream.pipe(res);
});

app.get("/artifact/image", (req, res) => {
  const path = req.query.path;
  res.sendFile(path);
});

app.get("/artifact/json", (req, res) => {
  const path = req.query.path;
  const contents = fs.readFileSync(path);
  try {
    const data = JSON.parse(contents);
    res.json(data);
  } catch (err) {
    res.status(500).send(err);
  }
});

app.get("/artifact/text", (req, res) => {
  const path = req.query.path;
  const contents = fs.readFileSync(path);
  res.send(contents);
});

app.listen(9090);
