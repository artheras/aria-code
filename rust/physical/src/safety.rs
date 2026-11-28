//! Continuous-space kinematic safety envelope & collision-clearance watchdog.
//!
//! Designed as a deterministic safety gate between high-level planners / VLAs
//! and downstream simulation (MuJoCo / Gazebo) or hardware (ROS 2 `/cmd_vel`).

use serde::{Deserialize, Serialize};

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
pub struct Pose2D {
    pub x: f64,
    pub y: f64,
    pub yaw_rad: f64,
}

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
pub struct Twist2D {
    pub linear_mps: f64,
    pub angular_rps: f64,
}

impl Twist2D {
    pub fn zero() -> Self {
        Self {
            linear_mps: 0.0,
            angular_rps: 0.0,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
pub struct ObstacleCircle {
    pub x: f64,
    pub y: f64,
    pub radius_m: f64,
}

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
pub struct SafetyEnvelope {
    pub max_linear_mps: f64,
    pub max_angular_rps: f64,
    pub max_accel_mps2: f64,
    pub footprint_radius_m: f64,
    pub min_clearance_m: f64,
    pub control_dt_s: f64,
}

impl Default for SafetyEnvelope {
    fn default() -> Self {
        Self {
            max_linear_mps: 1.5,
            max_angular_rps: 1.8,
            max_accel_mps2: 2.0,
            footprint_radius_m: 0.30,
            min_clearance_m: 0.20,
            control_dt_s: 0.10,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct SafetyVerdict {
    pub allowed: bool,
    pub clamped_twist: Twist2D,
    pub min_obstacle_clearance_m: Option<f64>,
    pub stopping_distance_m: f64,
    pub reason: String,
}

/// Validate and clamp a 2D velocity command against kinematic limits, braking
/// stopping distance, and circular obstacle clearance.
pub fn validate_velocity_command(
    pose: Pose2D,
    cmd: Twist2D,
    obstacles: &[ObstacleCircle],
    env: SafetyEnvelope,
) -> SafetyVerdict {
    if !pose.x.is_finite()
        || !pose.y.is_finite()
        || !pose.yaw_rad.is_finite()
        || !cmd.linear_mps.is_finite()
        || !cmd.angular_rps.is_finite()
    {
        return SafetyVerdict {
            allowed: false,
            clamped_twist: Twist2D::zero(),
            min_obstacle_clearance_m: None,
            stopping_distance_m: 0.0,
            reason: "non-finite pose or velocity command".into(),
        };
    }

    if env.max_linear_mps <= 0.0
        || env.max_angular_rps <= 0.0
        || env.max_accel_mps2 <= 0.0
        || env.footprint_radius_m < 0.0
        || env.min_clearance_m < 0.0
        || env.control_dt_s <= 0.0
    {
        return SafetyVerdict {
            allowed: false,
            clamped_twist: Twist2D::zero(),
            min_obstacle_clearance_m: None,
            stopping_distance_m: 0.0,
            reason: "invalid safety envelope parameters".into(),
        };
    }

    let clamped = Twist2D {
        linear_mps: cmd
            .linear_mps
            .clamp(-env.max_linear_mps, env.max_linear_mps),
        angular_rps: cmd
            .angular_rps
            .clamp(-env.max_angular_rps, env.max_angular_rps),
    };

    let v = clamped.linear_mps.abs();
    let stopping_distance_m = (v * v) / (2.0 * env.max_accel_mps2) + v * env.control_dt_s;

    let next_yaw = pose.yaw_rad + clamped.angular_rps * env.control_dt_s;
    let next_x = pose.x + clamped.linear_mps * env.control_dt_s * next_yaw.cos();
    let next_y = pose.y + clamped.linear_mps * env.control_dt_s * next_yaw.sin();

    let mut min_clearance: Option<f64> = None;
    for obs in obstacles {
        if !obs.x.is_finite() || !obs.y.is_finite() || obs.radius_m < 0.0 {
            continue;
        }
        let dist_next = ((next_x - obs.x).powi(2) + (next_y - obs.y).powi(2)).sqrt();
        let clearance = dist_next - (env.footprint_radius_m + obs.radius_m);
        min_clearance = Some(match min_clearance {
            Some(cur) => cur.min(clearance),
            None => clearance,
        });
    }

    if let Some(clearance) = min_clearance {
        if clearance < env.min_clearance_m + stopping_distance_m && v > 1e-6 {
            return SafetyVerdict {
                allowed: false,
                clamped_twist: Twist2D::zero(),
                min_obstacle_clearance_m: Some(clearance),
                stopping_distance_m,
                reason: format!(
                    "obstacle clearance {clearance:.3}m below required safety margin {:.3}m",
                    env.min_clearance_m + stopping_distance_m
                ),
            };
        }
    }

    SafetyVerdict {
        allowed: true,
        clamped_twist: clamped,
        min_obstacle_clearance_m: min_clearance,
        stopping_distance_m,
        reason: "ok".into(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn clamps_velocity_to_envelope_in_open_space() {
        let pose = Pose2D {
            x: 0.0,
            y: 0.0,
            yaw_rad: 0.0,
        };
        let cmd = Twist2D {
            linear_mps: 3.5,
            angular_rps: -4.0,
        };
        let env = SafetyEnvelope::default();
        let verdict = validate_velocity_command(pose, cmd, &[], env);
        assert!(verdict.allowed);
        assert_eq!(verdict.clamped_twist.linear_mps, env.max_linear_mps);
        assert_eq!(verdict.clamped_twist.angular_rps, -env.max_angular_rps);
        assert!(verdict.stopping_distance_m > 0.0);
    }

    #[test]
    fn blocks_motion_when_obstacle_inside_stopping_margin() {
        let pose = Pose2D {
            x: 0.0,
            y: 0.0,
            yaw_rad: 0.0,
        };
        let cmd = Twist2D {
            linear_mps: 1.2,
            angular_rps: 0.0,
        };
        let obstacles = [ObstacleCircle {
            x: 0.85,
            y: 0.0,
            radius_m: 0.25,
        }];
        let verdict = validate_velocity_command(pose, cmd, &obstacles, SafetyEnvelope::default());
        assert!(!verdict.allowed);
        assert_eq!(verdict.clamped_twist, Twist2D::zero());
        assert!(verdict.reason.contains("obstacle clearance"));
    }

    #[test]
    fn rejects_non_finite_inputs() {
        let pose = Pose2D {
            x: f64::NAN,
            y: 0.0,
            yaw_rad: 0.0,
        };
        let verdict =
            validate_velocity_command(pose, Twist2D::zero(), &[], SafetyEnvelope::default());
        assert!(!verdict.allowed);
        assert!(verdict.reason.contains("non-finite"));
    }
}
