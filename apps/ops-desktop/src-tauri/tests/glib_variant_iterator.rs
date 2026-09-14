use gtk::glib::prelude::*;

#[test]
fn variant_string_iteration_preserves_values_from_both_ends() {
    let value = ["alpha", "bêta", "", "delta"].to_variant();
    assert_eq!(value.array_iter_str().unwrap().collect::<Vec<_>>(), ["alpha", "bêta", "", "delta"]);
    let mut iter = value.array_iter_str().unwrap();
    assert_eq!(iter.next(), Some("alpha"));
    assert_eq!(iter.next_back(), Some("delta"));
    assert_eq!(iter.nth(1), Some(""));
    assert_eq!(iter.next(), None);
    assert_eq!(value.array_iter_str().unwrap().nth_back(1), Some(""));
    assert_eq!(value.array_iter_str().unwrap().last(), Some("delta"));
}
