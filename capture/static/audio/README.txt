Vendored audio assets land here (all local, nothing fetched):

  reference_tone.wav   - fixed calibration tone for task 2, bit-identical
                         every session
  demo_soft_pa.wav     - spoken demos, CONSONANT tasks only
  demo_pataka.wav
  demo_s_z.wav

Never add a demo for a vowel task (sustained_a, sustained_i, mpt): it would
anchor the participant's pitch, and fundamental frequency is one of the
measures. domain/tasks.py enforces the same rule on the config side.
