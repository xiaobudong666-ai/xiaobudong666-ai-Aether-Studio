/**
 * RationalTime handles frame-accurate timing using fraction math (value / timescale)
 * to avoid cumulative floating point precision issues.
 */
export class RationalTime {
  constructor(
    public readonly value: number,
    public readonly timescale: number
  ) {
    if (!Number.isSafeInteger(value) || !Number.isSafeInteger(timescale)) {
      throw new Error("RationalTime values must be safe integers");
    }
    if (timescale <= 0) {
      throw new Error("timescale must be greater than 0");
    }
  }

  static fromSeconds(seconds: number, timescale = 24000): RationalTime {
    if (!Number.isFinite(seconds)) {
      throw new Error("seconds must be finite");
    }
    return new RationalTime(Math.round(seconds * timescale), timescale);
  }

  static fromMilliseconds(ms: number, timescale = 1000): RationalTime {
    if (!Number.isFinite(ms)) {
      throw new Error("milliseconds must be finite");
    }
    return new RationalTime(Math.round(ms * (timescale / 1000)), timescale);
  }

  toSeconds(): number {
    return this.value / this.timescale;
  }

  toMilliseconds(): number {
    return (this.value / this.timescale) * 1000;
  }

  private static gcd(a: bigint, b: bigint): bigint {
    a = a < 0n ? -a : a;
    b = b < 0n ? -b : b;
    while (b !== 0n) {
      const remainder = a % b;
      a = b;
      b = remainder;
    }
    return a;
  }

  private static fromBigInts(value: bigint, timescale: bigint): RationalTime {
    if (timescale <= 0n) {
      throw new Error("timescale must be greater than 0");
    }
    const max = BigInt(Number.MAX_SAFE_INTEGER);
    const min = BigInt(Number.MIN_SAFE_INTEGER);
    if (value > max || value < min || timescale > max) {
      throw new Error("RationalTime result exceeds safe integer range");
    }
    return new RationalTime(Number(value), Number(timescale));
  }

  simplify(): RationalTime {
    const value = BigInt(this.value);
    const timescale = BigInt(this.timescale);
    const divisor = RationalTime.gcd(value, timescale);
    return RationalTime.fromBigInts(value / divisor, timescale / divisor);
  }

  add(other: RationalTime): RationalTime {
    if (this.timescale === other.timescale) {
      return RationalTime.fromBigInts(
        BigInt(this.value) + BigInt(other.value),
        BigInt(this.timescale)
      );
    }

    const leftScale = BigInt(this.timescale);
    const rightScale = BigInt(other.timescale);
    const scaleGcd = RationalTime.gcd(leftScale, rightScale);
    const leftMultiplier = rightScale / scaleGcd;
    const rightMultiplier = leftScale / scaleGcd;
    const commonTimescale = leftScale * leftMultiplier;
    const combinedValue =
      BigInt(this.value) * leftMultiplier +
      BigInt(other.value) * rightMultiplier;
    const resultGcd = RationalTime.gcd(combinedValue, commonTimescale);

    return RationalTime.fromBigInts(
      combinedValue / resultGcd,
      commonTimescale / resultGcd
    );
  }

  subtract(other: RationalTime): RationalTime {
    if (this.timescale === other.timescale) {
      return RationalTime.fromBigInts(
        BigInt(this.value) - BigInt(other.value),
        BigInt(this.timescale)
      );
    }

    const leftScale = BigInt(this.timescale);
    const rightScale = BigInt(other.timescale);
    const scaleGcd = RationalTime.gcd(leftScale, rightScale);
    const leftMultiplier = rightScale / scaleGcd;
    const rightMultiplier = leftScale / scaleGcd;
    const commonTimescale = leftScale * leftMultiplier;
    const combinedValue =
      BigInt(this.value) * leftMultiplier -
      BigInt(other.value) * rightMultiplier;
    const resultGcd = RationalTime.gcd(combinedValue, commonTimescale);

    return RationalTime.fromBigInts(
      combinedValue / resultGcd,
      commonTimescale / resultGcd
    );
  }

  equals(other: RationalTime): boolean {
    return this.compare(other) === 0;
  }

  greaterThan(other: RationalTime): boolean {
    return this.compare(other) > 0;
  }

  lessThan(other: RationalTime): boolean {
    return this.compare(other) < 0;
  }

  compare(other: RationalTime): -1 | 0 | 1 {
    const left = BigInt(this.value) * BigInt(other.timescale);
    const right = BigInt(other.value) * BigInt(this.timescale);
    if (left === right) return 0;
    return left < right ? -1 : 1;
  }

  toJSON() {
    return {
      value: this.value,
      timescale: this.timescale,
    };
  }
}

/**
 * 480p proxy specifications as constants
 */
export const PROXY_480P = {
  width: 854,
  height: 480,
  targetBitrateKbps: 1500,
  fps: 24,
  codec: "h264",
};

/**
 * Error Codes
 */
export enum ErrorCode {
  PROJECT_NOT_FOUND = "PROJECT_NOT_FOUND",
  CONCURRENCY_CONFLICT = "CONCURRENCY_CONFLICT",
  INVALID_TIMELINE = "INVALID_TIMELINE",
  TASK_FAILED = "TASK_FAILED",
  VALIDATION_ERROR = "VALIDATION_ERROR",
}

/**
 * SSE Events representation
 */
export interface TaskProgressPayload {
  taskId: string;
  projectId: string;
  progress: number; // 0 to 100
  status:
    | "queued"
    | "dispatching"
    | "processing"
    | "completed"
    | "failed"
    | "canceled"
    | "partial"
    | "unknown";
  canonicalStatus:
    | "QUEUED"
    | "RUNNING"
    | "SUCCEEDED"
    | "FAILED"
    | "CANCELED"
    | "PARTIAL"
    | "UNKNOWN";
  message: string;
  artifactUrl?: string;
  attempts?: number;
  createdAt?: string;
  updatedAt?: string;
  error?: string;
}

export type SSEEvent =
  | { type: "task_progress"; payload: TaskProgressPayload }
  | { type: "heartbeat"; payload: { timestamp: string } };

export * from "./schemas";
