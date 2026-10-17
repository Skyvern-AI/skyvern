import express from "express";
import fs from "fs";
import path from "path";
import cors from "cors";

const app = express();

app.use(cors());

export function getRecordingContentType(filePath) {
  const extension = path.extname(filePath).toLowerCase();

  switch (extension) {
    case ".webm":
      return "video/webm";
    case ".mp4":
      return "video/mp4";
    default:
      return "application/octet-stream";
  }
}

// Request logging middleware — logs method, path, status, and duration
app.use((req, res, next) => {
  const start = Date.now();
  res.on("finish", () => {
    const duration = Date.now() - start;
    const timestamp = new Date().toISOString();
    const artifactPath = req.query.path || "";
    console.log(
      "[%s] %s %s %d %dms %s",
      timestamp,
      req.method,
      req.path,
      res.statusCode,
      duration,
      artifactPath,
    );
  });
  next();
});

app.get("/artifact/recording", (req, res) => {
  const range = req.headers.range;
  const filePath = req.query.path;
  if (!filePath || !range) {
    return res.status(400).send("Missing path or range header");
  }
  const videoSize = fs.statSync(filePath).size;
  const chunkSize = 1 * 1e6;
  const start = Number(range.replace(/\D/g, ""));
  const end = Math.min(start + chunkSize, videoSize - 1);
  const contentLength = end - start + 1;
  const headers = {
    "Content-Range": `bytes ${start}-${end}/${videoSize}`,
    "Accept-Ranges": "bytes",
    "Content-Length": contentLength,
    "Content-Type": getRecordingContentType(filePath),
  };
  res.writeHead(206, headers);
  const stream = fs.createReadStream(filePath, {
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

// Error handling middleware — catches unhandled errors in routes
app.use((err, req, res, _next) => {
  const timestamp = new Date().toISOString();
  console.error(
    "[%s] ERROR %s %s:",
    timestamp,
    req.method,
    req.path,
    err.message,
  );
  res.status(500).send("Internal server error");
});

if (process.env.NODE_ENV !== "test") {
  app.listen(9090, () => {
    console.log(
      `[${new Date().toISOString()}] Artifact server running at http://localhost:9090`,
    );
  });
}
