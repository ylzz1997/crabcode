//! Test-only experiments. These do not advertise or enable a production input
//! provider. Run serially against the temporary fixture, never a live app.
//!
//! SPI layouts are hypotheses from Cua commit
//! 9bbfa7dd3e27ca7f1861ede70aaca390174493f9, input/skylight.rs and mouse.rs.
//! Unlike that recipe, this never defocuses the user's app or posts a primer
//! compatibility or preventive focus isolation.
use super::tests::MacInputTestHost;
use super::*;

type PostRecord = unsafe extern "C" fn(*const u32, *const u8) -> i32;
type PostMouse = unsafe extern "C" fn(i32, core_graphics::sys::CGEventRef);
type SetFront = unsafe extern "C" fn(*const u32, u32, u32) -> i32;

struct ExperimentApi {
    record: PostRecord,
    mouse: PostMouse,
    restore: SetFront,
}

impl ExperimentApi {
    fn load() -> Result<Self, String> {
        unsafe {
            let handle = libc::dlopen(
                c"/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight".as_ptr(),
                libc::RTLD_LAZY | libc::RTLD_LOCAL,
            );
            if handle.is_null() {
                return Err("SkyLight unavailable".into());
            }
            // Keep the framework loaded for the lifetime of these pointers.
            let record = libc::dlsym(handle, c"SLPSPostEventRecordTo".as_ptr());
            let mouse = libc::dlsym(handle, c"SLEventPostToPid".as_ptr());
            let restore = libc::dlsym(handle, c"SLPSSetFrontProcessWithOptions".as_ptr());
            if record.is_null() || mouse.is_null() || restore.is_null() {
                return Err("Required experimental SkyLight symbols unavailable".into());
            }
            Ok(Self {
                record: std::mem::transmute::<*mut libc::c_void, PostRecord>(record),
                mouse: std::mem::transmute::<*mut libc::c_void, PostMouse>(mouse),
                restore: std::mem::transmute::<*mut libc::c_void, SetFront>(restore),
            })
        }
    }
}

fn activation_record(window_id: u32, active: bool) -> [u8; 248] {
    let mut bytes = [0; 248];
    bytes[4] = 248;
    bytes[8] = 13;
    bytes[60..64].copy_from_slice(&window_id.to_le_bytes());
    bytes[138] = if active { 1 } else { 2 };
    bytes
}

struct ExperimentCleanup<'a> {
    api: &'a ExperimentApi,
    previous: [u32; 2],
    target: [u32; 2],
    window_id: u32,
}

impl Drop for ExperimentCleanup<'_> {
    fn drop(&mut self) {
        unsafe {
            (self.api.record)(
                self.target.as_ptr(),
                activation_record(self.window_id, false).as_ptr(),
            );
        }
        // Restore only if OUR fixture remains frontmost; never pull focus back
        // from an app the user independently switched to during the experiment.
        if mac_front_process_serial_number() == Ok(self.target) {
            unsafe {
                (self.api.restore)(self.previous.as_ptr(), 0, 0x400);
            }
        }
    }
}

#[test]
fn synthetic_record_targets_exactly_one_window() {
    let active = activation_record(0x12345678, true);
    let inactive = activation_record(0x12345678, false);
    assert_eq!(&active[60..64], &0x12345678u32.to_le_bytes());
    assert_eq!(
        active
            .iter()
            .zip(inactive)
            .filter(|(a, b)| **a != *b)
            .count(),
        1
    );
}

#[test]
#[ignore = "experimental private input; launches only temporary AppKit fixtures; run with --test-threads=1 --nocapture"]
fn compare_synthetic_background_input_on_fixture() {
    mac_require_input_permission().expect("Accessibility is required for the experimental fixture");
    let api = ExperimentApi::load().expect("Experimental SPI probe failed");
    for (name, fields, synthetic, skylight) in [
        ("quartz", false, false, false),
        ("quartz_fields", true, false, false),
        ("synthetic_quartz", false, true, false),
        ("synthetic_skylight_fields", true, true, true),
    ] {
        let fixture = MacInputTestHost::start_fixture(
            "tests/fixtures/scroll_host.swift",
            &["--require-active"],
        );
        let initial = (0..100)
            .find_map(|_| {
                let state = fixture.state();
                if state.is_none() {
                    thread::sleep(Duration::from_millis(50));
                }
                state
            })
            .expect("Fixture startup timed out");
        assert_eq!(initial["active"], false);
        let target = WindowTarget {
            pid: initial["pid"].as_i64().unwrap() as i32,
            window_id: initial["target_id"].as_u64().unwrap() as u32,
            x: initial["origin_x"].as_i64().unwrap() as i32,
            y: initial["origin_y"].as_i64().unwrap() as i32,
            width: 0,
            height: 0,
        };
        let previous = mac_front_process_serial_number().unwrap();
        let target_psn = mac_process_serial_number(target.pid).unwrap();
        assert_ne!(previous, target_psn);
        let cleanup = ExperimentCleanup {
            api: &api,
            previous,
            target: target_psn,
            window_id: target.window_id,
        };
        let monitor = MacForegroundMonitor::start(target.pid).unwrap();
        if synthetic {
            assert_eq!(
                unsafe {
                    (api.record)(
                        target_psn.as_ptr(),
                        activation_record(target.window_id, true).as_ptr(),
                    )
                },
                0
            );
            thread::sleep(Duration::from_millis(100));
        }
        let x = initial["x"].as_i64().unwrap() as i32;
        let y = initial["y"].as_i64().unwrap() as i32;
        let mut sent = false;
        // A synthetic record can itself cause activation. Stop the experiment
        // before a business click if that has already happened.
        if mac_front_process_serial_number() == Ok(previous) {
            let events = [
                CGEventType::MouseMoved,
                CGEventType::LeftMouseDown,
                CGEventType::LeftMouseUp,
            ]
            .into_iter()
            .enumerate()
            .map(|(i, kind)| {
                let event = mac_mouse_event(
                    target,
                    kind,
                    CGMouseButton::Left,
                    x,
                    y,
                    if i == 0 { 0 } else { 1 },
                )
                .unwrap();
                if fields {
                    for (field, value) in [
                        (0, if i == 0 { 2 } else { 3 }),
                        (7, 3),
                        (40, i64::from(target.pid)),
                        (58, 1),
                    ] {
                        event.set_integer_value_field(field, value);
                    }
                }
                event
            })
            .collect::<Vec<_>>();
            for event in &events {
                if skylight {
                    unsafe {
                        (api.mouse)(target.pid, event.as_ptr());
                    }
                } else {
                    mac_post_prepared_mouse(event, target.pid);
                }
                thread::sleep(Duration::from_millis(20));
            }
            sent = true;
        }
        thread::sleep(Duration::from_millis(300));
        let after = fixture.state().unwrap();
        let activated = monitor.finish().unwrap();
        eprintln!(
            "background experiment {}",
            json!({
                "variant": name, "action_dispatched": sent,
                "accepted_clicks": after["target_clicks"], "dropped_clicks": after["target_dropped_clicks"],
                "app_active": after["active"], "target_became_frontmost_sampled": activated,
                "window_raised_sampled": after["ever_raised"], "cursor_changed": after["cursor"] != initial["cursor"],
                "typing_focus_verified": false, "production_ready": false,
            })
        );
        assert!(
            after["target_clicks"].as_u64().unwrap() <= 1,
            "duplicate business click: {after}"
        );
        assert_eq!(after["decoy_clicks"], 0, "wrong window: {after}");
        drop(cleanup);
    }
}
