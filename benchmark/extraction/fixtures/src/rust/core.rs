//! Core fixture: nested definitions, duplicate names, multiline signatures.

/// Computes the outer value.
#[inline]
pub fn compute(
    a: i32,
    b: i32,
) -> i32 {
    a + b
}

pub mod inner {
    /// Shadows the outer `compute` name.
    pub fn compute() -> i32 {
        0
    }

    pub mod deep {
        pub fn compute() -> i32 {
            1
        }
    }
}
