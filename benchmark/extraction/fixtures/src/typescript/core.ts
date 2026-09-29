// Core fixture: nested definitions, duplicate names, multiline signatures, decorators.

/** Computes the outer value. */
function compute(
  a: number,
  b: number,
): number {
  return a + b;
}

class Service {
  @readonly
  value: number = 0;

  @logged
  run(): number {
    return this.value;
  }
}

namespace inner {
  /** Shadows the outer `compute` name. */
  export function compute(): number {
    return 0;
  }

  export namespace deep {
    export function compute(): number {
      return 1;
    }
  }
}
