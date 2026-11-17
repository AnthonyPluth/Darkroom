const ECC_PER_BLOCK = [10, 16, 26, 18, 24, 16, 18, 22, 22, 26];
const BLOCKS = [1, 1, 1, 2, 2, 4, 4, 4, 5, 5];
const MEDIUM_FORMAT = 0;
const MASKS = 8;
const RUN = 3;
const BOXES = 3;
const FINDER_LIKE = 40;
const BALANCE = 10;

export type QrMatrix = boolean[][];

type Bits = number[];

function append(bits: Bits, value: number, length: number): void {
  for (let i = length - 1; i >= 0; i--) bits.push((value >>> i) & 1);
}

function rawModules(version: number): number {
  let result = (16 * version + 128) * version + 64;
  if (version >= 2) {
    const aligned = Math.floor(version / 7) + 2;
    result -= (25 * aligned - 10) * aligned - 55;
    if (version >= 7) result -= 36;
  }
  return result;
}

function dataCodewords(version: number): number {
  return Math.floor(rawModules(version) / 8) - ECC_PER_BLOCK[version - 1] * BLOCKS[version - 1];
}

function payload(text: string, version: number): Bits {
  const bits: Bits = [];
  const bytes = new TextEncoder().encode(text);
  append(bits, 0b0100, 4);
  append(bits, bytes.length, version < 10 ? 8 : 16);
  bytes.forEach((b) => append(bits, b, 8));
  return bits;
}

function multiply(x: number, y: number): number {
  let z = 0;
  for (let i = 7; i >= 0; i--) {
    z = (z << 1) ^ ((z >>> 7) * 0x11d);
    z ^= ((y >>> i) & 1) * x;
  }
  return z;
}

function divisor(degree: number): number[] {
  const result = Array<number>(degree - 1).fill(0).concat([1]);
  let root = 1;
  for (let i = 0; i < degree; i++) {
    for (let j = 0; j < result.length; j++) {
      result[j] = multiply(result[j], root);
      if (j + 1 < result.length) result[j] ^= result[j + 1];
    }
    root = multiply(root, 0x02);
  }
  return result;
}

function remainder(data: number[], by: number[]): number[] {
  const result = Array<number>(by.length).fill(0);
  for (const b of data) {
    const factor = b ^ (result.shift() ?? 0);
    result.push(0);
    by.forEach((coefficient, i) => (result[i] ^= multiply(coefficient, factor)));
  }
  return result;
}

function withCorrection(data: number[], version: number): number[] {
  const count = BLOCKS[version - 1];
  const eccLength = ECC_PER_BLOCK[version - 1];
  const raw = Math.floor(rawModules(version) / 8);
  const short = count - (raw % count);
  const shortLength = Math.floor(raw / count);
  const by = divisor(eccLength);
  const blocks: number[][] = [];
  for (let i = 0, k = 0; i < count; i++) {
    const block = data.slice(k, k + shortLength - eccLength + (i < short ? 0 : 1));
    k += block.length;
    const ecc = remainder(block, by);
    if (i < short) block.push(0);
    blocks.push(block.concat(ecc));
  }
  const result: number[] = [];
  for (let i = 0; i < blocks[0].length; i++) {
    blocks.forEach((block, j) => {
      if (i !== shortLength - eccLength || j >= short) result.push(block[i]);
    });
  }
  return result;
}

function alignmentPositions(version: number, size: number): number[] {
  if (version === 1) return [];
  const count = Math.floor(version / 7) + 2;
  const step = Math.ceil((version * 4 + 4) / (count * 2 - 2)) * 2;
  const result = [6];
  for (let position = size - 7; result.length < count; position -= step) result.splice(1, 0, position);
  return result;
}

class Grid {
  readonly modules: QrMatrix;
  readonly fixed: boolean[][];
  readonly size: number;

  constructor(size: number) {
    this.size = size;
    this.modules = Array.from({ length: size }, () => Array<boolean>(size).fill(false));
    this.fixed = Array.from({ length: size }, () => Array<boolean>(size).fill(false));
  }

  set(x: number, y: number, dark: boolean): void {
    this.modules[y][x] = dark;
    this.fixed[y][x] = true;
  }

  finder(cx: number, cy: number): void {
    for (let dy = -4; dy <= 4; dy++) {
      for (let dx = -4; dx <= 4; dx++) {
        const distance = Math.max(Math.abs(dx), Math.abs(dy));
        const x = cx + dx;
        const y = cy + dy;
        if (x >= 0 && x < this.size && y >= 0 && y < this.size) this.set(x, y, distance !== 2 && distance !== 4);
      }
    }
  }

  alignment(cx: number, cy: number): void {
    for (let dy = -2; dy <= 2; dy++) for (let dx = -2; dx <= 2; dx++) this.set(cx + dx, cy + dy, Math.max(Math.abs(dx), Math.abs(dy)) !== 1);
  }

  format(mask: number): void {
    const data = (MEDIUM_FORMAT << 3) | mask;
    let rem = data;
    for (let i = 0; i < 10; i++) rem = (rem << 1) ^ ((rem >>> 9) * 0x537);
    const bits = ((data << 10) | rem) ^ 0x5412;
    const bit = (i: number): boolean => ((bits >>> i) & 1) !== 0;
    for (let i = 0; i <= 5; i++) this.set(8, i, bit(i));
    this.set(8, 7, bit(6));
    this.set(8, 8, bit(7));
    this.set(7, 8, bit(8));
    for (let i = 9; i < 15; i++) this.set(14 - i, 8, bit(i));
    for (let i = 0; i < 8; i++) this.set(this.size - 1 - i, 8, bit(i));
    for (let i = 8; i < 15; i++) this.set(8, this.size - 15 + i, bit(i));
    this.set(8, this.size - 8, true);
  }

  version(version: number): void {
    if (version < 7) return;
    let rem = version;
    for (let i = 0; i < 12; i++) rem = (rem << 1) ^ ((rem >>> 11) * 0x1f25);
    const bits = (version << 12) | rem;
    for (let i = 0; i < 18; i++) {
      const dark = ((bits >>> i) & 1) !== 0;
      const a = this.size - 11 + (i % 3);
      const b = Math.floor(i / 3);
      this.set(a, b, dark);
      this.set(b, a, dark);
    }
  }

  patterns(version: number): void {
    for (let i = 0; i < this.size; i++) {
      this.set(6, i, i % 2 === 0);
      this.set(i, 6, i % 2 === 0);
    }
    this.finder(3, 3);
    this.finder(this.size - 4, 3);
    this.finder(3, this.size - 4);
    const positions = alignmentPositions(version, this.size);
    const last = positions.length - 1;
    positions.forEach((y, i) => positions.forEach((x, j) => {
      if (!((i === 0 && j === 0) || (i === 0 && j === last) || (i === last && j === 0))) this.alignment(x, y);
    }));
    this.format(0);
    this.version(version);
  }

  codewords(data: number[]): void {
    let i = 0;
    for (let right = this.size - 1; right >= 1; right -= 2) {
      if (right === 6) right = 5;
      for (let vertical = 0; vertical < this.size; vertical++) {
        for (let j = 0; j < 2; j++) {
          const x = right - j;
          const upward = ((right + 1) & 2) === 0;
          const y = upward ? this.size - 1 - vertical : vertical;
          if (!this.fixed[y][x] && i < data.length * 8) {
            this.modules[y][x] = ((data[i >>> 3] >>> (7 - (i & 7))) & 1) !== 0;
            i++;
          }
        }
      }
    }
  }

  mask(mask: number): void {
    for (let y = 0; y < this.size; y++) {
      for (let x = 0; x < this.size; x++) {
        if (!this.fixed[y][x] && masked(mask, x, y)) this.modules[y][x] = !this.modules[y][x];
      }
    }
  }

  penalty(): number {
    let result = 0;
    for (let a = 0; a < this.size; a++) {
      result += this.line((b) => this.modules[a][b]);
      result += this.line((b) => this.modules[b][a]);
    }
    for (let y = 0; y < this.size - 1; y++) {
      for (let x = 0; x < this.size - 1; x++) {
        const c = this.modules[y][x];
        if (c === this.modules[y][x + 1] && c === this.modules[y + 1][x] && c === this.modules[y + 1][x + 1]) result += BOXES;
      }
    }
    const dark = this.modules.reduce((sum, row) => sum + row.filter(Boolean).length, 0);
    const total = this.size * this.size;
    return result + (Math.ceil(Math.abs(dark * 20 - total * 10) / total) - 1) * BALANCE;
  }

  private line(at: (i: number) => boolean): number {
    let result = 0;
    let colour = false;
    let run = 0;
    const history = Array<number>(7).fill(0);
    for (let i = 0; i < this.size; i++) {
      if (at(i) === colour) {
        run++;
        if (run === 5) result += RUN;
        else if (run > 5) result++;
      } else {
        this.remember(run, history);
        if (!colour) result += this.finderLike(history) * FINDER_LIKE;
        colour = at(i);
        run = 1;
      }
    }
    if (colour) {
      this.remember(run, history);
      run = 0;
    }
    this.remember(run + this.size, history);
    return result + this.finderLike(history) * FINDER_LIKE;
  }

  private remember(run: number, history: number[]): void {
    history.pop();
    history.unshift(history[0] === 0 ? run + this.size : run);
  }

  private finderLike(h: number[]): number {
    const n = h[1];
    const core = n > 0 && h[2] === n && h[3] === n * 3 && h[4] === n && h[5] === n;
    return (core && h[0] >= n * 4 && h[6] >= n ? 1 : 0) + (core && h[6] >= n * 4 && h[0] >= n ? 1 : 0);
  }
}

function masked(mask: number, x: number, y: number): boolean {
  switch (mask) {
    case 0: return (x + y) % 2 === 0;
    case 1: return y % 2 === 0;
    case 2: return x % 3 === 0;
    case 3: return (x + y) % 3 === 0;
    case 4: return (Math.floor(x / 3) + Math.floor(y / 2)) % 2 === 0;
    case 5: return ((x * y) % 2) + ((x * y) % 3) === 0;
    case 6: return (((x * y) % 2) + ((x * y) % 3)) % 2 === 0;
    default: return (((x + y) % 2) + ((x * y) % 3)) % 2 === 0;
  }
}

export function qrMatrix(text: string, forcedMask?: number): QrMatrix {
  let version = 1;
  let bits = payload(text, version);
  while (bits.length > dataCodewords(version) * 8) {
    version++;
    if (version > ECC_PER_BLOCK.length) throw new RangeError("too long for a QR code");
    bits = payload(text, version);
  }
  const capacity = dataCodewords(version) * 8;
  append(bits, 0, Math.min(4, capacity - bits.length));
  append(bits, 0, (8 - (bits.length % 8)) % 8);
  for (let pad = 0xec; bits.length < capacity; pad ^= 0xec ^ 0x11) append(bits, pad, 8);
  const data: number[] = [];
  for (let i = 0; i < bits.length; i += 8) data.push(bits.slice(i, i + 8).reduce((byte, bit) => (byte << 1) | bit, 0));
  const grid = new Grid(version * 4 + 17);
  grid.patterns(version);
  grid.codewords(withCorrection(data, version));
  let chosen = forcedMask ?? 0;
  if (forcedMask === undefined) {
    let lowest = Infinity;
    for (let mask = 0; mask < MASKS; mask++) {
      grid.mask(mask);
      grid.format(mask);
      const penalty = grid.penalty();
      if (penalty < lowest) {
        lowest = penalty;
        chosen = mask;
      }
      grid.mask(mask);
    }
  }
  grid.mask(chosen);
  grid.format(chosen);
  return grid.modules;
}
