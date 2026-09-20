import { readFileSync as readFile } from "fs";
import * as path from "path";

export { compute as computeAlias } from "./core";

export default function wrapped(): number {
  return path.sep.length + readFile.length;
}
