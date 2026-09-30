Feature: Search and safe editing through MCP
  Background:
    Given an example workspace

  Scenario: Search reflects a tagged edit in the same session
    When I search for "offset"
    Then the search resolves "offset" in "src/helpers.rs" at line 1
      """
      pub fn offset() -> u8 {
          7
      }
      """
    When I search for "greeting_message"
    Then the search resolves "greeting_message" in "src/lib.rs" at line 3
      """
      pub fn greeting_message() -> u8 {
          helpers::offset()
      }
      """
    When I read "src/lib.rs"
    And I replace "greeting_message" with "welcome_message" using the read tag
    Then the edit is applied
    When I search for "welcome_message"
    Then the search resolves "welcome_message" in "src/lib.rs" at line 3
      """
      pub fn welcome_message() -> u8 {
          helpers::offset()
      }
      """
    When I search for "greeting_message"
    Then the search has no matches
    And the file "src/lib.rs" contains exactly
      """
      mod helpers;

      pub fn welcome_message() -> u8 {
          helpers::offset()
      }

      pub fn untouched_value() -> u8 {
          9
      }
      """

  Scenario: A tagged edit preserves an independent external change
    When I read "src/lib.rs"
    And another editor replaces "9" with "10" in "src/lib.rs"
    And I replace "greeting_message" with "welcome_message" using the read tag
    Then the edit is applied
    And the file "src/lib.rs" contains exactly
      """
      mod helpers;

      pub fn welcome_message() -> u8 {
          helpers::offset()
      }

      pub fn untouched_value() -> u8 {
          10
      }
      """

  Scenario: A conflicting external change rejects the tagged edit
    When I read "src/lib.rs"
    And another editor replaces "greeting_message" with "external_message" in "src/lib.rs"
    And I replace "greeting_message" with "welcome_message" using the read tag
    Then the conflicting edit is rejected
    And the edited file is unchanged
    And the file "src/lib.rs" contains exactly
      """
      mod helpers;

      pub fn external_message() -> u8 {
          helpers::offset()
      }

      pub fn untouched_value() -> u8 {
          9
      }
      """
