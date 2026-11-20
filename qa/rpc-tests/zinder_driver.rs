// Copyright (c) 2026 The Zcash developers
// Distributed under the MIT software license.

#[cfg(test)]
mod chain {
    mod tests {
        use serde_json::Value;
        use std::{
            env,
            ffi::OsString,
            fs,
            io::{self, Write as _},
            ops::Range,
            path::{Path, PathBuf},
        };
        use zallet_core::{
            components::{
                bounded_scan_certification::{
                    BoundedScanCertificationConfig, BoundedScanCertificationOutcome,
                    certify_bounded_scan,
                },
                chain::ChainError,
            },
            config::ZalletConfig,
        };
        use zallet_zinder::{
            ZinderChain, open_zinder_index, probe_missing_wallet_runtime_capabilities,
        };
        use zinder_client::{Capability, IndexerError, Network as ZinderNetwork};
        const RANGE_BARRIER_DIRECTORY_ENV: &str = "ZIT_RANGE_BARRIER_DIR";
        const RANGE_REQUEST_PAUSE_START_HEIGHT_ENV: &str = "ZIT_RANGE_REQUEST_PAUSE_START_HEIGHT";
        const RANGE_REQUEST_PAUSED_MARKER_FILENAME: &str = "range-request-paused.json";
        const CONTINUE_RANGE_REQUEST_MARKER_FILENAME: &str = "continue-range-request";
        const CERTIFICATION_EVIDENCE_SCHEMA_VERSION: u32 = 1;
        const ZINDER_ENDPOINT_ENV: &str = "ZIT_ZINDER_ENDPOINT";
        const ZALLET_CONFIG_ENV: &str = "ZIT_ZALLET_CONFIG";
        const CERTIFICATION_DATADIR_ENV: &str = "ZIT_CERTIFICATION_DATADIR";
        const REQUESTED_START_HEIGHT_ENV: &str = "ZIT_REQUESTED_START_HEIGHT";
        const REQUESTED_END_HEIGHT_EXCLUSIVE_ENV: &str = "ZIT_REQUESTED_END_HEIGHT_EXCLUSIVE";
        const RETRY_END_HEIGHT_EXCLUSIVE_ENV: &str = "ZIT_RETRY_END_HEIGHT_EXCLUSIVE";
        const CERTIFICATION_RESULT_ENV: &str = "ZIT_CERTIFICATION_RESULT";
        const EPOCH_PIN_UNAVAILABLE_SOURCE_CLASSIFICATION: &str =
            "IndexerError::ChainEpochPinUnavailable";

        type CertificationTestResult<T = ()> = Result<T, Box<dyn std::error::Error>>;

        struct CertificationEnvironment {
            zinder_endpoint: String,
            zallet_config: ZalletConfig,
            certification_datadir: PathBuf,
            requested_block_range: Range<u32>,
            certification_result_path: PathBuf,
        }

        #[derive(Debug, Eq, PartialEq)]
        struct RangeRequestMarker {
            schema_version: u32,
            attempt_number: u64,
            requested_start_height_inclusive: u32,
            requested_end_height_inclusive: u32,
            chain_epoch_id: u64,
        }

        fn read_certification_environment() -> CertificationTestResult<CertificationEnvironment> {
            let zinder_endpoint = required_environment_text(ZINDER_ENDPOINT_ENV)?;
            let zallet_config_path = required_absolute_environment_path(ZALLET_CONFIG_ENV)?;
            let zallet_config_contents =
                fs::read_to_string(&zallet_config_path).map_err(|error| {
                    certification_failure(format!(
                        "cannot read Zallet configuration {zallet_config_path:?}: {error}"
                    ))
                })?;
            let zallet_config = toml::from_str(&zallet_config_contents).map_err(|error| {
                certification_failure(format!(
                    "cannot parse Zallet configuration {zallet_config_path:?}: {error}"
                ))
            })?;
            let requested_start_height = required_environment_height(REQUESTED_START_HEIGHT_ENV)?;
            let requested_end_height =
                required_environment_height(REQUESTED_END_HEIGHT_EXCLUSIVE_ENV)?;
            let certification_result_path =
                required_absolute_environment_path(CERTIFICATION_RESULT_ENV)?;
            if certification_result_path.try_exists()? {
                return Err(certification_failure(format!(
                    "{CERTIFICATION_RESULT_ENV} must not replace stale evidence: \
             {certification_result_path:?} already exists"
                ))
                .into());
            }

            Ok(CertificationEnvironment {
                zinder_endpoint,
                zallet_config,
                certification_datadir: required_absolute_environment_path(
                    CERTIFICATION_DATADIR_ENV,
                )?,
                requested_block_range: requested_start_height..requested_end_height,
                certification_result_path,
            })
        }

        fn required_environment_text(name: &'static str) -> io::Result<String> {
            match env::var(name) {
                Ok(value) if !value.is_empty() => Ok(value),
                Ok(_) => Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    format!("required environment variable {name} is empty"),
                )),
                Err(env::VarError::NotPresent) => Err(io::Error::new(
                    io::ErrorKind::NotFound,
                    format!("required environment variable {name} is missing"),
                )),
                Err(env::VarError::NotUnicode(_)) => Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    format!("{name} must contain valid UTF-8"),
                )),
            }
        }

        fn required_absolute_environment_path(name: &'static str) -> io::Result<PathBuf> {
            let path = match env::var_os(name) {
                Some(path) if !path.is_empty() => PathBuf::from(path),
                Some(_) => Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    format!("required environment variable {name} is empty"),
                ))?,
                None => {
                    return Err(io::Error::new(
                        io::ErrorKind::NotFound,
                        format!("required environment variable {name} is missing"),
                    ));
                }
            };
            if !path.is_absolute() {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    format!("{name} must be an absolute path: {path:?}"),
                ));
            }

            Ok(path)
        }

        fn required_environment_height(name: &'static str) -> io::Result<u32> {
            let encoded_height = required_environment_text(name)?;
            encoded_height.parse().map_err(|error| {
                io::Error::new(
                    io::ErrorKind::InvalidInput,
                    format!(
                        "{name} must be a decimal u32 height, received {encoded_height:?}: {error}"
                    ),
                )
            })
        }

        fn certification_failure(message: impl Into<String>) -> io::Error {
            io::Error::other(message.into())
        }

        fn sqlite_wallet_artifact_paths(wallet_database_path: &Path) -> [PathBuf; 3] {
            [
                wallet_database_path.to_owned(),
                path_with_suffix(wallet_database_path, "-wal"),
                path_with_suffix(wallet_database_path, "-shm"),
            ]
        }

        fn path_with_suffix(path: &Path, suffix: &str) -> PathBuf {
            let mut suffixed_path = OsString::from(path);
            suffixed_path.push(suffix);
            PathBuf::from(suffixed_path)
        }

        fn find_existing_wallet_artifacts(
            wallet_artifact_paths: &[PathBuf],
        ) -> io::Result<Vec<PathBuf>> {
            wallet_artifact_paths
                .iter()
                .filter_map(|path| match path.try_exists() {
                    Ok(true) => Some(Ok(path.clone())),
                    Ok(false) => None,
                    Err(error) => Some(Err(error)),
                })
                .collect()
        }

        fn is_wallet_artifact_path(path: &Path, wallet_artifact_paths: &[PathBuf]) -> bool {
            wallet_artifact_paths
                .iter()
                .any(|wallet_artifact_path| path == wallet_artifact_path)
        }

        fn read_range_request_marker(path: &Path) -> io::Result<RangeRequestMarker> {
            let encoded_marker = fs::read(path)?;
            let marker: Value =
                serde_json::from_slice(&encoded_marker).map_err(io::Error::other)?;
            let schema_version =
                u32::try_from(required_marker_u64(&marker, path, "schema_version")?).map_err(
                    |error| {
                        certification_failure(format!(
                            "range-request marker {path:?} has an invalid schema_version: {error}"
                        ))
                    },
                )?;
            let requested_start_height_inclusive = u32::try_from(required_marker_u64(
                &marker,
                path,
                "requested_start_height_inclusive",
            )?)
            .map_err(|error| {
                certification_failure(format!(
                    "range-request marker {path:?} has an invalid start height: {error}"
                ))
            })?;
            let requested_end_height_inclusive = u32::try_from(required_marker_u64(
                &marker,
                path,
                "requested_end_height_inclusive",
            )?)
            .map_err(|error| {
                certification_failure(format!(
                    "range-request marker {path:?} has an invalid end height: {error}"
                ))
            })?;

            Ok(RangeRequestMarker {
                schema_version,
                attempt_number: required_marker_u64(&marker, path, "attempt_number")?,
                requested_start_height_inclusive,
                requested_end_height_inclusive,
                chain_epoch_id: required_marker_u64(&marker, path, "chain_epoch_id")?,
            })
        }

        fn required_marker_u64(
            marker: &Value,
            path: &Path,
            field: &'static str,
        ) -> io::Result<u64> {
            marker.get(field).and_then(Value::as_u64).ok_or_else(|| {
                certification_failure(format!(
                    "range-request marker {path:?} is missing unsigned integer field {field:?}"
                ))
            })
        }

        fn assert_range_request_marker(
            marker: &RangeRequestMarker,
            attempt_number: u64,
            requested_block_range: &Range<u32>,
        ) -> io::Result<()> {
            let expected_marker = RangeRequestMarker {
                schema_version: CERTIFICATION_EVIDENCE_SCHEMA_VERSION,
                attempt_number,
                requested_start_height_inclusive: requested_block_range.start,
                requested_end_height_inclusive: requested_block_range.end - 1,
                chain_epoch_id: marker.chain_epoch_id,
            };
            if marker != &expected_marker {
                return Err(certification_failure(format!(
                    "range-request marker differs from the expected attempt and inclusive range: \
             actual {marker:?}, expected {expected_marker:?}"
                )));
            }

            Ok(())
        }

        fn optional_environment_u32(name: &'static str) -> io::Result<Option<u32>> {
            match env::var(name) {
                Ok(value) => parse_u32_environment_value(name, &value).map(Some),
                Err(env::VarError::NotPresent) => Ok(None),
                Err(env::VarError::NotUnicode(_)) => Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    format!("{name} must contain valid UTF-8"),
                )),
            }
        }

        fn parse_u32_environment_value(name: &'static str, value: &str) -> io::Result<u32> {
            value.parse::<u32>().map_err(|error| {
                io::Error::new(
                    io::ErrorKind::InvalidInput,
                    format!("{name} must be a decimal u32, received {value:?}: {error}"),
                )
            })
        }

        fn range_request_attempt_marker_path(
            barrier_directory: &Path,
            attempt_number: u64,
        ) -> std::path::PathBuf {
            barrier_directory.join(format!("range-request-attempt-{attempt_number}.json"))
        }

        fn write_json_atomically(path: &Path, evidence: &Value) -> io::Result<()> {
            let parent_directory = path.parent().ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::InvalidInput,
                    format!("JSON evidence path has no parent directory: {path:?}"),
                )
            })?;
            fs::create_dir_all(parent_directory)?;
            let mut temporary_file = tempfile::NamedTempFile::new_in(parent_directory)?;
            serde_json::to_writer_pretty(&mut temporary_file, evidence)
                .map_err(io::Error::other)?;
            temporary_file.write_all(b"\n")?;
            temporary_file.as_file().sync_all()?;
            temporary_file.persist_noclobber(path).map_err(|error| {
                io::Error::new(
                    error.error.kind(),
                    format!(
                        "cannot persist JSON evidence without replacing {path:?}: {}",
                        error.error
                    ),
                )
            })?;
            Ok(())
        }

        #[tokio::test(flavor = "multi_thread")]
        #[ignore = "requires an externally orchestrated Zinder certification runtime"]
        async fn endpoint_without_full_blocks_fails_before_wallet_open() -> CertificationTestResult
        {
            let certification_environment = read_certification_environment()?;
            let certification_config = BoundedScanCertificationConfig::new(
                &certification_environment.certification_datadir,
                certification_environment.zallet_config.clone(),
                certification_environment.requested_block_range.clone(),
            )?;
            let wallet_artifact_paths =
                sqlite_wallet_artifact_paths(&certification_config.wallet_database_path());
            if is_wallet_artifact_path(
                &certification_environment.certification_result_path,
                &wallet_artifact_paths,
            ) {
                return Err(certification_failure(format!(
            "{CERTIFICATION_RESULT_ENV} must not alias wallet.db or its WAL/SHM sidecars: {:?}",
            certification_environment.certification_result_path
        ))
        .into());
            }
            let wallet_artifacts_before = find_existing_wallet_artifacts(&wallet_artifact_paths)?;
            if !wallet_artifacts_before.is_empty() {
                return Err(certification_failure(format!(
                    "negative certification wallet artifacts already exist before admission: \
             {wallet_artifacts_before:?}"
                ))
                .into());
            }

            let params = certification_environment.zallet_config.consensus.network();
            let index = open_zinder_index(
                certification_environment.zinder_endpoint.clone(),
                ZinderNetwork::ZcashRegtest,
            )?;
            let missing_capabilities = probe_missing_wallet_runtime_capabilities(&index).await?;
            let expected_missing_capabilities =
                vec![Capability::FullBlock, Capability::FullBlockRange];
            if missing_capabilities != expected_missing_capabilities {
                return Err(certification_failure(format!(
            "Transactions endpoint missing-capability list differs from the exact wallet-runtime \
             requirement order: actual {missing_capabilities:?}, \
             expected {expected_missing_capabilities:?}"
        ))
        .into());
            }

            let connection_error =
                match ZinderChain::connect(certification_environment.zinder_endpoint, params).await
                {
                    Ok(_) => {
                        return Err(certification_failure(
                            "Transactions endpoint unexpectedly passed wallet-runtime admission",
                        )
                        .into());
                    }
                    Err(error) => error,
                };
            let expected_rejection = "raw_blob_policy=all";
            if !connection_error.to_string().contains(expected_rejection) {
                return Err(certification_failure(format!(
                    "ZinderChain::connect rejected for an unexpected reason: {connection_error}"
                ))
                .into());
            }

            let wallet_artifacts_after = find_existing_wallet_artifacts(&wallet_artifact_paths)?;
            if !wallet_artifacts_after.is_empty() {
                return Err(certification_failure(format!(
                    "negative certification created wallet artifacts despite failed admission: \
             {wallet_artifacts_after:?}"
                ))
                .into());
            }

            let missing_capability_names = missing_capabilities
                .iter()
                .map(Capability::as_str)
                .collect::<Vec<_>>();
            let wallet_artifact_path_names = wallet_artifact_paths
                .iter()
                .map(|path| path.display().to_string())
                .collect::<Vec<_>>();
            let evidence = serde_json::json!({
                "schema_version": CERTIFICATION_EVIDENCE_SCHEMA_VERSION,
                "missing_capabilities": missing_capability_names,
                "connection_rejected": true,
                "wallet_artifact_paths": wallet_artifact_path_names,
                "wallet_artifacts_absent_before_admission": true,
                "wallet_artifacts_absent_after_admission": true,
            });
            write_json_atomically(
                &certification_environment.certification_result_path,
                &evidence,
            )?;
            let wallet_artifacts_after_result_write =
                find_existing_wallet_artifacts(&wallet_artifact_paths)?;
            if !wallet_artifacts_after_result_write.is_empty() {
                return Err(certification_failure(format!(
                    "negative certification result persistence created wallet artifacts: \
             {wallet_artifacts_after_result_write:?}"
                ))
                .into());
            }

            Ok(())
        }

        #[tokio::test(flavor = "multi_thread")]
        #[ignore = "requires an externally orchestrated Zinder certification runtime"]
        async fn endpoint_certifies_birthday_through_tip() -> CertificationTestResult {
            let certification_environment = read_certification_environment()?;
            let certification_config = BoundedScanCertificationConfig::new(
                &certification_environment.certification_datadir,
                certification_environment.zallet_config.clone(),
                certification_environment.requested_block_range,
            )?;
            let chain = ZinderChain::connect(
                certification_environment.zinder_endpoint,
                certification_environment.zallet_config.consensus.network(),
            )
            .await?;
            let certification_outcome = certify_bounded_scan(&chain, &certification_config).await?;
            let certification_evidence = match certification_outcome {
                BoundedScanCertificationOutcome::Certified(evidence) => evidence,
                BoundedScanCertificationOutcome::ChainViewExpired { .. } => {
                    return Err(certification_failure(
                        "bounded scan unexpectedly expired instead of certifying the endpoint",
                    )
                    .into());
                }
                _ => {
                    return Err(certification_failure(
                        "bounded scan returned an unsupported certification outcome",
                    )
                    .into());
                }
            };
            let certification_evidence = serde_json::to_value(certification_evidence)?;
            write_json_atomically(
                &certification_environment.certification_result_path,
                &certification_evidence,
            )?;

            Ok(())
        }

        #[tokio::test(flavor = "multi_thread")]
        #[ignore = "requires an externally orchestrated Zinder certification runtime"]
        async fn expired_epoch_reacquires_complete_bounded_scan() -> CertificationTestResult {
            let certification_environment = read_certification_environment()?;
            let retry_end_height = required_environment_height(RETRY_END_HEIGHT_EXCLUSIVE_ENV)?;
            let expected_retry_end_height = certification_environment
                .requested_block_range
                .end
                .checked_add(1)
                .ok_or_else(|| {
                    certification_failure(format!(
                        "{REQUESTED_END_HEIGHT_EXCLUSIVE_ENV} cannot advance by one block"
                    ))
                })?;
            if retry_end_height != expected_retry_end_height {
                return Err(certification_failure(format!(
                    "{RETRY_END_HEIGHT_EXCLUSIVE_ENV} must equal \
             {REQUESTED_END_HEIGHT_EXCLUSIVE_ENV} + 1 for one-block epoch rotation: \
             received {retry_end_height}, expected {expected_retry_end_height}"
                ))
                .into());
            }
            let barrier_directory =
                required_absolute_environment_path(RANGE_BARRIER_DIRECTORY_ENV)?;
            if optional_environment_u32(RANGE_REQUEST_PAUSE_START_HEIGHT_ENV)?
                != Some(certification_environment.requested_block_range.start)
            {
                return Err(certification_failure(format!(
                    "{RANGE_REQUEST_PAUSE_START_HEIGHT_ENV} must equal \
             {REQUESTED_START_HEIGHT_ENV} for epoch-expiry certification"
                ))
                .into());
            }

            let first_attempt_marker_path =
                range_request_attempt_marker_path(&barrier_directory, 1);
            let second_attempt_marker_path =
                range_request_attempt_marker_path(&barrier_directory, 2);
            let paused_marker_path = barrier_directory.join(RANGE_REQUEST_PAUSED_MARKER_FILENAME);
            let continue_range_request_path =
                barrier_directory.join(CONTINUE_RANGE_REQUEST_MARKER_FILENAME);
            let barrier_paths = [
                &first_attempt_marker_path,
                &second_attempt_marker_path,
                &paused_marker_path,
                &continue_range_request_path,
            ];
            for barrier_path in barrier_paths {
                if barrier_path.try_exists()? {
                    return Err(certification_failure(format!(
                        "epoch-expiry certification requires a fresh barrier directory; \
                 stale path exists: {barrier_path:?}"
                    ))
                    .into());
                }
            }

            let first_requested_block_range =
                certification_environment.requested_block_range.clone();
            let first_certification_config = BoundedScanCertificationConfig::new(
                &certification_environment.certification_datadir,
                certification_environment.zallet_config.clone(),
                first_requested_block_range.clone(),
            )?;
            let chain = ZinderChain::connect(
                certification_environment.zinder_endpoint,
                certification_environment.zallet_config.consensus.network(),
            )
            .await?;
            let first_outcome = certify_bounded_scan(&chain, &first_certification_config).await?;
            let (chain_error, expiry_evidence) = match first_outcome {
                BoundedScanCertificationOutcome::ChainViewExpired {
                    chain_error,
                    evidence,
                } => (chain_error, evidence),
                BoundedScanCertificationOutcome::Certified(_) => {
                    return Err(certification_failure(
                "first bounded scan unexpectedly certified instead of observing epoch expiry",
            )
            .into());
                }
                _ => {
                    return Err(certification_failure(
                        "first bounded scan returned an unsupported certification outcome",
                    )
                    .into());
                }
            };
            let ChainError::ViewExpired(expiry_source) = chain_error else {
                return Err(certification_failure(
                    "expired certification outcome did not retain ChainError::ViewExpired",
                )
                .into());
            };
            let Some(indexer_error) = expiry_source.downcast_ref::<IndexerError>() else {
                return Err(certification_failure(
                    "expired view source did not downcast to zinder_client::IndexerError",
                )
                .into());
            };
            if !matches!(indexer_error, IndexerError::ChainEpochPinUnavailable) {
                return Err(certification_failure(format!(
                    "expired view retained the wrong IndexerError variant: {indexer_error:?}"
                ))
                .into());
            }
            if expiry_evidence.block_metadata_before != expiry_evidence.block_metadata_after {
                return Err(certification_failure(format!(
                    "expired bounded scan changed block metadata: before {:?}, after {:?}",
                    expiry_evidence.block_metadata_before, expiry_evidence.block_metadata_after
                ))
                .into());
            }
            if !expiry_evidence.block_metadata_before.is_empty()
                || !expiry_evidence.block_metadata_after.is_empty()
            {
                return Err(certification_failure(format!(
                    "fresh rotation wallet contained block metadata around the expired attempt: \
             before {:?}, after {:?}",
                    expiry_evidence.block_metadata_before, expiry_evidence.block_metadata_after
                ))
                .into());
            }

            let retry_requested_block_range = first_requested_block_range.start..retry_end_height;
            let retry_certification_config = BoundedScanCertificationConfig::new(
                &certification_environment.certification_datadir,
                certification_environment.zallet_config,
                retry_requested_block_range.clone(),
            )?;
            let retry_outcome = certify_bounded_scan(&chain, &retry_certification_config).await?;
            let retry_evidence = match retry_outcome {
                BoundedScanCertificationOutcome::Certified(evidence) => evidence,
                BoundedScanCertificationOutcome::ChainViewExpired { .. } => {
                    return Err(certification_failure(
                        "retry bounded scan expired instead of certifying the replacement epoch",
                    )
                    .into());
                }
                _ => {
                    return Err(certification_failure(
                        "retry bounded scan returned an unsupported certification outcome",
                    )
                    .into());
                }
            };

            let first_attempt_marker = read_range_request_marker(&first_attempt_marker_path)?;
            let second_attempt_marker = read_range_request_marker(&second_attempt_marker_path)?;
            assert_range_request_marker(&first_attempt_marker, 1, &first_requested_block_range)?;
            assert_range_request_marker(&second_attempt_marker, 2, &retry_requested_block_range)?;
            if first_attempt_marker.chain_epoch_id == second_attempt_marker.chain_epoch_id {
                return Err(certification_failure(format!(
                    "bounded-scan retry reused chain epoch {} instead of reacquiring a new epoch",
                    first_attempt_marker.chain_epoch_id
                ))
                .into());
            }

            let evidence = serde_json::json!({
                "schema_version": CERTIFICATION_EVIDENCE_SCHEMA_VERSION,
                "expiry_evidence": expiry_evidence,
                "expiry_source_classification": EPOCH_PIN_UNAVAILABLE_SOURCE_CLASSIFICATION,
                "retry_evidence": retry_evidence,
                "range_request_attempts": [
                    {
                        "attempt_number": first_attempt_marker.attempt_number,
                        "chain_epoch_id": first_attempt_marker.chain_epoch_id,
                        "requested_start_height_inclusive":
                            first_attempt_marker.requested_start_height_inclusive,
                        "requested_end_height_inclusive":
                            first_attempt_marker.requested_end_height_inclusive,
                    },
                    {
                        "attempt_number": second_attempt_marker.attempt_number,
                        "chain_epoch_id": second_attempt_marker.chain_epoch_id,
                        "requested_start_height_inclusive":
                            second_attempt_marker.requested_start_height_inclusive,
                        "requested_end_height_inclusive":
                            second_attempt_marker.requested_end_height_inclusive,
                    },
                ],
            });
            write_json_atomically(
                &certification_environment.certification_result_path,
                &evidence,
            )?;

            Ok(())
        }
    }
}
