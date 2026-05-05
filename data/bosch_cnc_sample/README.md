# Bundled Bosch CNC Sample

This folder contains a compact, attributed sample from the real Bosch CNC Machining Dataset so the simulator can replay genuine industrial measurements out of the box.

## Source

- UCI dataset page: [Bosch CNC Machining Dataset](https://archive.ics.uci.edu/dataset/752/bosch%2Bcnc%2Bmachining%2Bdataset)
- Original repository: [boschresearch/CNC_Machining](https://github.com/boschresearch/CNC_Machining)
- Paper: [Smart Data Collection System for Brownfield CNC Milling Machines](https://doi.org/10.1016/j.procir.2022.04.022)

## Included Files

- `M01/OP01/good/M01_Aug_2019_OP01_000.h5`
- `M01/OP01/bad/M01_Aug_2019_OP01_000.h5`
- `M02/OP03/good/M02_Aug_2019_OP03_000.h5`
- `M03/OP05/good/M03_Aug_2019_OP05_000.h5`

These files remain under the dataset’s published attribution requirements. The upstream repository describes the data as real tri-axial accelerometer traces collected from brownfield CNC milling machines at 2 kHz, with `good` and `bad` labels for process health.
