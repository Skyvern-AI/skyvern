type BarcodeDetectorConstructor = new (options?: { formats?: string[] }) => {
  detect: (image: ImageBitmap) => Promise<Array<{ rawValue?: string }>>;
};

type BrowserBarcodeDetectorScope = typeof globalThis & {
  BarcodeDetector?: BarcodeDetectorConstructor;
};

const QR_SCAN_UNSUPPORTED_MESSAGE =
  "QR scanning is not supported by this browser. Paste the setup key or otpauth:// URI instead.";
const QR_CODE_NOT_FOUND_MESSAGE =
  "No QR code was found in that image. Try a clearer screenshot or paste the setup key.";

async function decodeQrCodeImage(file: File): Promise<string> {
  const scope = globalThis as BrowserBarcodeDetectorScope;
  const BarcodeDetector = scope.BarcodeDetector;

  if (!BarcodeDetector) {
    throw new Error(QR_SCAN_UNSUPPORTED_MESSAGE);
  }

  let detector: InstanceType<BarcodeDetectorConstructor>;
  try {
    detector = new BarcodeDetector({ formats: ["qr_code"] });
  } catch {
    throw new Error(QR_SCAN_UNSUPPORTED_MESSAGE);
  }

  let image: ImageBitmap;
  try {
    image = await globalThis.createImageBitmap(file);
  } catch {
    throw new Error(QR_SCAN_UNSUPPORTED_MESSAGE);
  }

  try {
    const codes = await detector.detect(image);
    const rawValue = codes.find((code) => code.rawValue)?.rawValue?.trim();
    if (!rawValue) {
      throw new Error(QR_CODE_NOT_FOUND_MESSAGE);
    }
    return rawValue;
  } finally {
    image.close();
  }
}

export { decodeQrCodeImage };
