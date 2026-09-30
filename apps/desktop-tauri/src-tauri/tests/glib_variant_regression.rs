#![cfg(target_os = "linux")]

use glib::variant::ToVariant;

#[test]
fn string_iterator_preserves_all_read_paths_under_optimization() {
    let cases = vec![
        Vec::<String>::new(),
        vec![String::new()],
        vec!["singleton".into()],
        vec![String::new(), "Unicode 中文 🦀".into(), "x".repeat(65536)],
    ];
    for _ in 0..128 {
        for values in &cases {
            let variant = values.to_variant();
            let expected: Vec<&str> = values.iter().map(String::as_str).collect();
            assert_eq!(
                variant.array_iter_str().unwrap().collect::<Vec<_>>(),
                expected
            );
            assert_eq!(
                variant.array_iter_str().unwrap().rev().collect::<Vec<_>>(),
                expected.iter().rev().copied().collect::<Vec<_>>()
            );
            assert_eq!(
                variant.array_iter_str().unwrap().last(),
                expected.last().copied()
            );
            let mut alternating = variant.array_iter_str().unwrap();
            let (mut head, mut tail) = (0, expected.len());
            while head < tail {
                assert_eq!(alternating.len(), tail - head);
                assert_eq!(alternating.next(), Some(expected[head]));
                head += 1;
                if head < tail {
                    tail -= 1;
                    assert_eq!(alternating.next_back(), Some(expected[tail]));
                }
            }
            assert_eq!(alternating.next(), None);
            assert_eq!(alternating.next_back(), None);
            for index in 0..=expected.len() {
                assert_eq!(
                    variant.array_iter_str().unwrap().nth(index),
                    expected.get(index).copied()
                );
                assert_eq!(
                    variant.array_iter_str().unwrap().nth_back(index),
                    expected.iter().rev().nth(index).copied()
                );
            }
            assert_eq!(variant.array_iter_str().unwrap().nth(usize::MAX), None);
            assert_eq!(variant.array_iter_str().unwrap().nth_back(usize::MAX), None);
        }
    }
}
