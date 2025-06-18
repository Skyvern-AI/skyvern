import express from "express";
import fs from "fs";
import cors from "cors";
import path from "path";

const app = express();

app.use(cors());

/**
 * GET /artifact/recording
 * Streams a video file with proper range support.
 * Expects query parameter: path (absolute file path).
 */
app.get("/artifact/recording", (req, res) => {
  const filePath = req.query.path;
  const range = req.headers.range;

  // Defensive: Check for required parameters
  if (!filePath) {
    res.status(400).send("Missing 'path' query parameter.");
    return;
  }
  if (!range) {
    res.status(400).send("Missing 'Range' header. This endpoint requires range requests.");
    return;
  }

  // Prevent directory traversal
  if (filePath.includes("..")) {
    res.status(400).send("Invalid file path.");
    return;
  }

  // Check file existence and get size safely
  let videoSize;
  try {
    videoSize = fs.statSync(filePath).size;
  } catch (err) {
    res.status(404).send("File not found.");
    return;
  }

  // Parse range header
  const matches = range.match(/bytes=(\d+)-(\d*)/);
  if (!matches) {
    res.status(416).send("Invalid Range header.");
    return;
  }
  const start = parseInt(matches[1], 10);
  const end = matches[2] ? Math.min(parseInt(matches[2], 10), videoSize - 1) : videoSize - 1;

  if (start > end || start < 0 || end >= videoSize) {
    res.status(416).send("Requested Range Not Satisfiable");
    return;
  }

  const chunkSize = end - start + 1;

  // Guess content type from extension
  const ext = path.extname(filePath).toLowerCase();
  let contentType = "application/octet-stream";
  if (ext === ".mp4") contentType = "video/mp4";
  else if (ext === ".webm") contentType = "video/webm";
  else if (ext === ".mov") contentType = "video/quicktime";

  const headers = {
    "Content-Range": `bytes ${start}-${end}/${videoSize}`,
    "Accept-Ranges": "bytes",
    "Content-Length": chunkSize,
    "Content-Type": contentType,
  };

  res.writeHead(206, headers);

  const stream = fs.createReadStream(filePath, { start, end });
  stream.pipe(res);

  stream.on("error", (err) => {
    res.status(500).send("Error reading file.");
  });
});

app.get("/artifact/image", (req, res) => {
  const filePath = req.query.path;
  if (!filePath) {
    res.status(400).send("Missing 'path' query parameter.");
    return;
  }
  if (filePath.includes("..")) {
    res.status(400).send("Invalid file path.");
    return;
  }
  res.sendFile(filePath, (err) => {
    if (err) {
      res.status(404).send("File not found.");
    }
  });
});

app.listen(9090);
