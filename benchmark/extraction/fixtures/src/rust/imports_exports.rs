use std::collections::HashMap as Map;
use std::fmt::{Display, Debug};

pub use crate::inner::compute as reexported_compute;

pub fn use_map() -> Map<String, i32> {
    Map::new()
}
