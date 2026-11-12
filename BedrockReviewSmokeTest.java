import java.util.List;

public class BedrockReviewSmokeTest {

    private OwnerRepository ownerRepository;

    public void loadOwners(List<Long> ownerIds) {
        for (Long id : ownerIds) {
            ownerRepository.findById(id);
        }
    }

    interface OwnerRepository {
        Object findById(Long id);
    }
}
