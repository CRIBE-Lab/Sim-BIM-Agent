from PIL import Image, ImageDraw

from sim_bim.drawing.images import candidate_regions, ink_profile


def test_candidate_regions_separate_drawings_and_ignore_the_frame() -> None:
    sheet = Image.new("L", (4000, 2000), 255)
    draw = ImageDraw.Draw(sheet)
    draw.rectangle([20, 20, 3980, 1980], outline=0, width=6)
    draw.rectangle([300, 300, 1500, 1500], outline=0, width=8)
    draw.line([300, 1540, 1500, 1540], fill=0, width=4)
    draw.rectangle([2300, 400, 3600, 1400], outline=0, width=8)

    regions = candidate_regions(sheet)

    assert len(regions) == 2
    left, right = regions
    assert left[0] <= 300 and left[2] >= 1500 and left[3] >= 1540
    assert right[0] <= 2300 and right[2] >= 3600


def test_ink_profile_finds_both_faces_of_a_wall() -> None:
    image = Image.new("L", (200, 100), 255)
    ImageDraw.Draw(image).rectangle([90, 0, 92, 99], fill=0)
    ImageDraw.Draw(image).rectangle([108, 0, 110, 99], fill=0)

    runs = ink_profile(image, (50, 50), (150, 50))

    assert [round(x) for x, _ in runs] == [91, 109]
