/**
 * RationalTime handles frame-accurate timing using fraction math (value / timescale)
 * to avoid cumulative floating point precision issues.
 */
export class RationalTime {
  constructor(
    public readonly value: number,
    public readonly timescale: number
  ) {
    if (timescale <= 0) {
      throw new Error("timescale must be greater than 0");
    }
    if (!Number.isSafeInteger(value) || !Number.isSafeInteger(timescale)) {
      throw new Error("RationalTime values must be safe integers");
    }
  }

  static fromSeconds(seconds: number, timescale = 24000): RationalTime {
    return new RationalTime(Math.round(seconds * timescale), timescale);
  }

  static fromMilliseconds(ms: number, timescale = 1000): RationalTime {
    return new RationalTime(Math.round(ms * (timescale / 1000)), timescale);
  }

  toSeconds(): number {
    return this.value / this.timescale;
  }

  toMilliseconds(): number {
    return (this.value / this.timescale) * 1000;
  }

  // Find Greatest Common Divisor to simplify fractions
  private gcd(a: number, b: number): number {
    a = Math.abs(a);
    b = Math.abs(b);
    while (b) {
      const t = b;
      b = a % b;
      a = t;
    }
    return a;
  }

  simplify(): RationalTime {
    const divisor = this.gcd(this.value, this.timescale);
    return new RationalTime(this.value / divisor, this.timescale / divisor);
  }

  add(other: RationalTime): RationalTime {
    if (this.timescale === other.timescale) {
      return new RationalTime(this.value + other.value, this.timescale);
    }
    const commonTimescale = this.timescale * other.timescale;
    const selfValue = this.value * other.timescale;
    const otherValue = other.value * this.timescale;
    return new RationalTime(selfValue + otherValue, commonTimescale).simplify();
  }

  subtract(other: RationalTime): RationalTime {
    if (this.timescale === other.timescale) {
      return new RationalTime(this.value - other.value, this.timescale);
    }
    const commonTimescale = this.timescale * other.timescale;
    const selfValue = this.value * other.timescale;
    const otherValue = other.value * this.timescale;
    return new RationalTime(selfValue - otherValue, commonTimescale).simplify();
  }

  equals(other: RationalTime): boolean {
    const s1 = this.simplify();
    const s2 = other.simplify();
    return s1.value === s2.value && s1.timescale === s2.timescale;
  }

  greaterThan(other: RationalTime): boolean {
    return this.toSeconds() > other.toSeconds();
  }

  lessThan(other: RationalTime): boolean {
    return this.toSeconds() < other.toSeconds();
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
  status: "pending" | "processing" | "completed" | "failed";
  message: string;
}

export type SSEEvent =
  | { type: "task_progress"; payload: TaskProgressPayload }
  | { type: "heartbeat"; payload: { timestamp: string } };

export * from "./schemas";
